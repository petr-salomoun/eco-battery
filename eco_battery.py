#!/usr/bin/env python3
"""
eco-battery: Smart battery charging based on grid demand curves.
Charges more when grid demand is low, less when demand is high.
"""

import gi
import warnings

# Suppress deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

# Try to import AppIndicator (optional)
HAS_APPINDICATOR = False
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    HAS_APPINDICATOR = True
except (ValueError, ImportError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3
        HAS_APPINDICATOR = True
    except (ValueError, ImportError):
        pass

import json
import random
from datetime import datetime
from pathlib import Path

# Fallback demand curve used only if curves.json cannot be loaded.
# Based on a typical central European (AT/DE) weekday annual average.
DEFAULT_CURVE = {
    0: 64, 1: 61, 2: 58, 3: 57, 4: 58, 5: 63,
    6: 74, 7: 86, 8: 95, 9: 100, 10: 100, 11: 98,
    12: 94, 13: 93, 14: 93, 15: 93, 16: 94, 17: 96,
    18: 96, 19: 94, 20: 90, 21: 85, 22: 78, 23: 70
}

CONFIG_DIR = Path.home() / ".config" / "eco-battery"
CONFIG_FILE = CONFIG_DIR / "config.json"
CURVES_FILE = Path("/usr/share/eco-battery/curves.json")


def load_curves():
    """Load demand curves from system file or use defaults."""
    if CURVES_FILE.exists():
        try:
            with open(CURVES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    # Also check local file for development
    local_curves = Path(__file__).parent / "data" / "curves.json"
    if local_curves.exists():
        try:
            with open(local_curves) as f:
                return json.load(f)
        except Exception:
            pass
    return {"default": DEFAULT_CURVE}


def load_config():
    """Load user configuration."""
    default = {"min_charge": 40, "max_charge": 95, "country": "AT"}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                return {**default, **cfg}
        except Exception:
            pass
    return default


def save_config(config):
    """Save user configuration."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def get_battery_path():
    """Find battery sysfs path. Returns path or None."""
    for bat in ["BAT0", "BAT1"]:
        path = Path(f"/sys/class/power_supply/{bat}")
        if (path / "charge_control_end_threshold").exists() or \
           (path / "charge_stop_threshold").exists() or \
           (path / "charge_behaviour").exists():
            return path
    return None


def _threshold_file_pairs(bat):
    """Return list of (stop_file, start_file_or_None) pairs for all interfaces on this battery."""
    pairs = []
    for stop_name, start_name in [
        ("charge_control_end_threshold", "charge_control_start_threshold"),
        ("charge_stop_threshold",        "charge_start_threshold"),
    ]:
        stop = bat / stop_name
        if stop.exists():
            start = bat / start_name
            pairs.append((stop, start if start.exists() else None))
    return pairs


def _try_write(path, value):
    """Write value to a sysfs file, falling back to pkexec on PermissionError."""
    try:
        with open(path, 'w') as f:
            f.write(str(value))
    except PermissionError:
        import subprocess
        subprocess.run(['pkexec', 'tee', str(path)],
                       input=str(value).encode(), capture_output=True, check=True)


def set_charge_threshold(threshold):
    """Write charge threshold to all available sysfs interfaces.

    Uses a narrow 2 % start/end window (start = threshold - 2) which is
    sufficient to let the EC re-engage charging after a brief discharge.
    Returns (success, error_message).
    """
    bat = get_battery_path()
    if not bat:
        return False, "no battery path"

    errors = []
    for stop_file, start_file in _threshold_file_pairs(bat):
        new_start = max(threshold - 2, 0)
        try:
            if start_file:
                try:
                    current_end = int(stop_file.read_text().strip())
                except Exception:
                    current_end = 100
                if threshold < current_end:
                    # Lowering: decrease start first to keep start < end at all times
                    _try_write(start_file, new_start)
                    _try_write(stop_file, threshold)
                else:
                    # Raising: increase end first
                    _try_write(stop_file, threshold)
                    _try_write(start_file, new_start)
            else:
                _try_write(stop_file, threshold)
        except Exception as e:
            errors.append(f"{stop_file.name}: {e}")

    if errors:
        return False, "; ".join(errors)
    return True, None


def set_charge_behaviour(behaviour):
    """Write to charge_behaviour sysfs file if present. behaviour is 'auto' or 'force-discharge'.

    force-discharge: battery drains even on AC until explicitly set back to auto.
    auto:            normal EC-controlled charging (respects threshold files).
    Returns (success, error_message).
    """
    bat = get_battery_path()
    if not bat:
        return False, "no battery path"
    f = bat / "charge_behaviour"
    if not f.exists():
        return False, "charge_behaviour not available"
    try:
        _try_write(f, behaviour)
        return True, None
    except Exception as e:
        return False, str(e)


def get_battery_info():
    """Get battery level and status."""
    bat = get_battery_path()
    if not bat:
        return None, None, None
    try:
        level = int((bat / "capacity").read_text().strip())
        status = (bat / "status").read_text().strip()
        threshold = int((bat / "charge_control_end_threshold").read_text().strip())
        return level, status, threshold
    except Exception:
        return None, None, None


def _next_peak_value(hour, curve):
    """Return the demand value of the next local maximum on the 24-h circular curve.

    Searches forward from `hour` (wrapping) until the demand stops rising.
    A 'local maximum' is the first hour where demand is higher than both its
    neighbours on the circular curve.  If the curve is monotone (no peak found
    in a full lap), the global maximum is returned as a fallback.
    """
    # Search up to 24 hours ahead (full cycle).
    for offset in range(1, 25):
        h_prev = (hour + offset - 1) % 24
        h_cur  = (hour + offset)     % 24
        h_next = (hour + offset + 1) % 24
        if curve[h_cur] >= curve[h_prev] and curve[h_cur] >= curve[h_next]:
            return curve[h_cur]
    # Fallback: global maximum
    return max(curve.values())


def calculate_target(hour, min_charge, max_charge, curve, peak_margin=2):
    """Return the battery target level for the given hour.

    Rule:
      - If current demand is more than `peak_margin` points below the next
        upcoming peak, we are in a valley / rising shoulder → target max_charge
        so the battery fills up before the peak arrives.
      - Otherwise (within `peak_margin` of the upcoming peak, i.e. at or near
        the peak) → target min_charge so the battery discharges and reduces
        load exactly when the grid needs it most.

    This produces a binary charge schedule anchored to actual peak timing
    rather than a continuous proportional mapping, which means the battery
    charges well before demand climbs and discharges right at the peak.
    Double-hump profiles are handled naturally: each local peak has its own
    preceding valley that qualifies as a charge window.

    `peak_margin` (default 2) is expressed in the same 0-100 units as the
    demand curve values.  It prevents adding charging load in the last hour(s)
    before a peak tip when the battery should already be full.
    """
    next_peak = _next_peak_value(hour, curve)
    demand    = curve.get(hour, curve.get(str(hour), 50))
    if demand <= next_peak - peak_margin:
        return max_charge   # valley / approach → fill up
    else:
        return min_charge   # at or near peak → discharge / hold low


class EcoBattery:
    """Main application."""
    
    def __init__(self):
        self.config = load_config()
        self.curves = load_curves()
        self.force_full = False
        
        # Create indicator or status icon
        if HAS_APPINDICATOR:
            self.indicator = AppIndicator3.Indicator.new(
                "eco-battery", "battery-good-charging",
                AppIndicator3.IndicatorCategory.SYSTEM_SERVICES
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self._build_menu())
            self.use_appindicator = True
        else:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name("battery-good-charging")
            self.status_icon.set_tooltip_text("eco-battery")
            self.status_icon.connect("popup-menu", self._on_popup_menu)
            self.use_appindicator = False
        
        # _update() runs every minute so that force-discharge can be stopped
        # promptly once the battery level drops to the threshold.
        # The threshold recalculation itself is cheap and only changes hourly anyway.
        GLib.timeout_add_seconds(60, self._tick)
        self._update()
    
    def _build_menu(self):
        menu = Gtk.Menu()
        
        self.status_item = Gtk.MenuItem(label="Status: --")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        
        self.battery_item = Gtk.MenuItem(label="Battery: --")
        self.battery_item.set_sensitive(False)
        menu.append(self.battery_item)
        
        menu.append(Gtk.SeparatorMenuItem())
        
        self.force_item = Gtk.MenuItem(label="⚡ Charge to 100%")
        self.force_item.connect("activate", self._on_force_full)
        menu.append(self.force_item)
        
        settings_item = Gtk.MenuItem(label="⚙️ Settings")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)
        
        menu.append(Gtk.SeparatorMenuItem())
        
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)
        
        menu.show_all()
        return menu
    
    def _on_popup_menu(self, status_icon, button, activate_time):
        """For Gtk.StatusIcon fallback."""
        menu = self._build_menu()
        menu.popup(None, None, Gtk.StatusIcon.position_menu, status_icon, button, activate_time)
    
    def _set_icon(self, icon_name):
        """Set icon - works for both AppIndicator and StatusIcon."""
        if self.use_appindicator:
            # Use set_icon_full to avoid deprecation warning
            self.indicator.set_icon_full(icon_name, "eco-battery")
        else:
            self.status_icon.set_from_icon_name(icon_name)
    
    def _tick(self):
        """Called every minute. Handles discharge monitoring and UI refresh."""
        self._update()
        return GLib.SOURCE_CONTINUE

    def _update(self):
        hour = datetime.now().hour
        curve = self.curves.get(self.config["country"], DEFAULT_CURVE)

        if isinstance(list(curve.keys())[0], str):
            curve = {int(k): v for k, v in curve.items()}

        if self.force_full:
            target = 100
            status_text = "⚡ Force charging to 100%"
            icon_name = "battery-full-charging"
        else:
            target = calculate_target(
                hour, self.config["min_charge"], self.config["max_charge"], curve
            )
            demand = curve.get(hour, 50)
            next_peak = _next_peak_value(hour, curve)
            at_peak = demand > next_peak - 2
            phase = "peak 📉" if at_peak else "valley 📈"
            status_text = f"Grid demand: {demand}% ({phase}) → Target: {target}%"

            if target >= 90:
                icon_name = "battery-full-charging"
            elif target >= 70:
                icon_name = "battery-good-charging"
            else:
                icon_name = "battery-low-charging"

        # Update icon and status label
        self._set_icon(icon_name)
        if not self.use_appindicator:
            self.status_icon.set_tooltip_text(f"eco-battery: {status_text}")
        self.status_item.set_label(status_text)

        # Write threshold files
        ok, err = set_charge_threshold(target)
        if not ok:
            self.status_item.set_label(f"Write error: {err}")

        # Read current battery state
        level, bat_status, _ = get_battery_info()

        # Manage charge_behaviour for discharge control:
        #   - If level is above the target, force-discharge until it drops to target.
        #   - Once at or below target, switch back to auto so normal charging can resume.
        # This is a no-op on hardware that doesn't expose charge_behaviour.
        if level is not None:
            bat = get_battery_path()
            behaviour_file = bat / "charge_behaviour" if bat else None
            if behaviour_file and behaviour_file.exists():
                if level > target:
                    set_charge_behaviour("force-discharge")
                else:
                    set_charge_behaviour("auto")

        # Update battery info label
        if level is not None:
            bat = get_battery_path()
            discharging_to_target = (
                bat_status == "Discharging"
                and bat is not None
                and (bat / "charge_behaviour").exists()
                and level > target
            )
            if discharging_to_target:
                charge_note = f"discharging to {target}%"
            elif bat_status == "Discharging":
                charge_note = "discharging"
            elif bat_status == "Charging":
                charge_note = f"charging to {target}%"
            elif bat_status in ("Not charging", "Full") and level >= target:
                charge_note = "at limit"
            else:
                charge_note = (bat_status or "unknown").lower()
            self.battery_item.set_label(f"Battery: {level}% ({charge_note})")
    
    def _on_force_full(self, widget):
        self.force_full = not self.force_full
        self.force_item.set_label("✓ Charging to 100% (click to cancel)" if self.force_full else "⚡ Charge to 100%")
        self._update()
    
    def _on_settings(self, widget):
        dialog = Gtk.Dialog(title="eco-battery Settings", flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                          Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dialog.set_default_size(300, 150)
        
        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        
        hbox1 = Gtk.Box(spacing=10)
        hbox1.pack_start(Gtk.Label(label="Maximum charge %:"), True, True, 0)
        max_spin = Gtk.SpinButton.new_with_range(60, 100, 5)
        max_spin.set_value(self.config["max_charge"])
        hbox1.pack_start(max_spin, False, False, 0)
        box.pack_start(hbox1, False, False, 0)
        
        hbox2 = Gtk.Box(spacing=10)
        hbox2.pack_start(Gtk.Label(label="Minimum charge %:"), True, True, 0)
        min_spin = Gtk.SpinButton.new_with_range(20, 80, 5)
        min_spin.set_value(self.config["min_charge"])
        hbox2.pack_start(min_spin, False, False, 0)
        box.pack_start(hbox2, False, False, 0)
        
        hbox3 = Gtk.Box(spacing=10)
        hbox3.pack_start(Gtk.Label(label="Demand curve:"), True, True, 0)
        country_combo = Gtk.ComboBoxText()
        for c in sorted(self.curves.keys()):
            country_combo.append(c, c)
        country_combo.set_active_id(self.config["country"])
        if country_combo.get_active() == -1:
            country_combo.set_active(0)
        hbox3.pack_start(country_combo, False, False, 0)
        box.pack_start(hbox3, False, False, 0)
        
        dialog.show_all()
        
        if dialog.run() == Gtk.ResponseType.OK:
            self.config["min_charge"] = int(min_spin.get_value())
            self.config["max_charge"] = int(max_spin.get_value())
            self.config["country"] = country_combo.get_active_text() or "default"
            save_config(self.config)
            self._update()
        
        dialog.destroy()
    
    def _cleanup(self):
        """Ensure force-discharge is never left active when the app exits."""
        set_charge_behaviour("auto")

    def _on_quit(self, widget=None):
        self._cleanup()
        Gtk.main_quit()

    def run(self):
        import signal
        # Handle SIGTERM (e.g. session logout, systemd stop) the same as a menu Quit.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_quit)
        Gtk.main()


def show_error_and_exit(message):
    """Show a GTK error dialog and exit. Falls back to stderr if GTK is unavailable."""
    try:
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="eco-battery: unsupported hardware",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
    except Exception:
        print(f"Error: {message}", file=__import__('sys').stderr)


def main():
    if not get_battery_path():
        show_error_and_exit(
            "No compatible battery found.\n\n"
            "eco-battery requires a laptop with kernel support for\n"
            "charge_control_end_threshold (e.g. ThinkPad via thinkpad_acpi,\n"
            "or other laptops with a supported battery driver).\n\n"
            "To check: ls /sys/class/power_supply/BAT*/charge_control_end_threshold\n\n"
            "ThinkPad users: sudo modprobe thinkpad_acpi"
        )
        return 1
    
    app = EcoBattery()
    app.run()
    return 0


if __name__ == "__main__":
    exit(main())
