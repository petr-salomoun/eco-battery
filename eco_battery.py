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
    0: 34, 1: 30, 2: 27, 3: 26, 4: 27, 5: 33,
    6: 48, 7: 68, 8: 83, 9: 88, 10: 87, 11: 85,
    12: 82, 13: 80, 14: 79, 15: 81, 16: 86, 17: 93,
    18: 100, 19: 96, 20: 86, 21: 72, 22: 55, 23: 41
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
    default = {"min_charge": 40, "max_charge": 100, "country": "AT"}
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
    """Find battery sysfs path."""
    for bat in ["BAT0", "BAT1"]:
        path = Path(f"/sys/class/power_supply/{bat}")
        if (path / "charge_control_end_threshold").exists():
            return path
    return None


def set_charge_threshold(threshold):
    """Set battery charge threshold."""
    bat = get_battery_path()
    if not bat:
        return False
    
    stop_file = bat / "charge_control_end_threshold"
    start_file = bat / "charge_control_start_threshold"
    
    def _write_sysfs(path, value):
        with open(path, 'w') as f:
            f.write(str(value))

    try:
        new_start = max(threshold - 5, 0)
        if start_file.exists():
            # Must write start before end when lowering, to keep start < end at all times.
            # When raising, write end first. Compare against current end to decide order.
            try:
                current_end = int(stop_file.read_text().strip())
            except Exception:
                current_end = 100
            if threshold < current_end:
                # Lowering: decrease start first so it stays below the (not yet lowered) end
                _write_sysfs(start_file, new_start)
                _write_sysfs(stop_file, threshold)
            else:
                # Raising: increase end first so it stays above the (not yet raised) start
                _write_sysfs(stop_file, threshold)
                _write_sysfs(start_file, new_start)
        else:
            _write_sysfs(stop_file, threshold)
        return True
    except PermissionError:
        import subprocess
        def _pkexec_write(path, value):
            subprocess.run(['pkexec', 'tee', str(path)],
                           input=str(value).encode(), capture_output=True, check=True)
        try:
            new_start = max(threshold - 5, 0)
            if start_file.exists():
                try:
                    current_end = int(stop_file.read_text().strip())
                except Exception:
                    current_end = 100
                if threshold < current_end:
                    _pkexec_write(start_file, new_start)
                    _pkexec_write(stop_file, threshold)
                else:
                    _pkexec_write(stop_file, threshold)
                    _pkexec_write(start_file, new_start)
            else:
                _pkexec_write(stop_file, threshold)
            return True
        except Exception:
            return False


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


def calculate_threshold(hour, min_charge, max_charge, curve):
    """Calculate charge threshold inversely proportional to demand."""
    demand = curve.get(hour, curve.get(str(hour), 50))
    min_demand = min(curve.values())
    max_demand = max(curve.values())
    
    if max_demand == min_demand:
        normalized = 0.5
    else:
        normalized = (demand - min_demand) / (max_demand - min_demand)
    
    threshold = max_charge - int(normalized * (max_charge - min_charge))
    return threshold


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
        
        # Pick a random offset within the hour so not all devices update simultaneously.
        # The first _update() runs immediately; the hourly cycle starts after the offset.
        offset = random.randint(0, 3599)
        GLib.timeout_add_seconds(offset, self._start_hourly_cycle)
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
        quit_item.connect("activate", lambda _: Gtk.main_quit())
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
    
    def _start_hourly_cycle(self):
        """Called once after the random startup offset; fires _update and starts the exact 1 h repeat."""
        self._update()
        GLib.timeout_add_seconds(3600, self._hourly_tick)
        return GLib.SOURCE_REMOVE  # one-shot

    def _hourly_tick(self):
        self._update()
        return GLib.SOURCE_CONTINUE  # repeat every 3600 s

    def _update(self):
        hour = datetime.now().hour
        curve = self.curves.get(self.config["country"], DEFAULT_CURVE)
        
        if isinstance(list(curve.keys())[0], str):
            curve = {int(k): v for k, v in curve.items()}
        
        if self.force_full:
            threshold = 100
            status_text = "⚡ Force charging to 100%"
            icon_name = "battery-full-charging"
        else:
            threshold = calculate_threshold(
                hour, self.config["min_charge"], self.config["max_charge"], curve
            )
            demand = curve.get(hour, 50)
            status_text = f"Grid demand: {demand}% → Charge to: {threshold}%"
            
            if threshold >= 90:
                icon_name = "battery-full-charging"
            elif threshold >= 70:
                icon_name = "battery-good-charging"
            else:
                icon_name = "battery-low-charging"
        
        # Update icon
        self._set_icon(icon_name)
        
        if not self.use_appindicator:
            self.status_icon.set_tooltip_text(f"eco-battery: {status_text}")
        
        # Update menu items
        self.status_item.set_label(status_text)
        
        # Set threshold
        set_charge_threshold(threshold)
        
        # Update battery info
        level, bat_status, _ = get_battery_info()
        if level is not None:
            self.battery_item.set_label(f"Battery: {level}% ({bat_status})")
    
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
    
    def run(self):
        Gtk.main()


def main():
    if not get_battery_path():
        print("Error: No compatible battery found.")
        print("Make sure thinkpad_acpi is loaded: sudo modprobe thinkpad_acpi")
        return 1
    
    app = EcoBattery()
    app.run()
    return 0


if __name__ == "__main__":
    exit(main())
