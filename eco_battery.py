#!/usr/bin/env python3
"""
eco-battery: Smart battery charging scheduled around electricity grid demand.

Charges at demand valleys, discharges at peaks, holds between transitions.
Good for the grid, good for the battery.

Manual mode: user-defined daily checkpoints (HH:MM + target %) override the
automatic schedule.  The battery is held at each checkpoint's target until the
next checkpoint's time arrives.
"""

import gi
import signal
import subprocess
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
    default = {
        "min_charge": 40,
        "max_charge": 95,
        "country": "AT",
        "manual_mode": False,
        "checkpoints": [],
    }
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
    """Get battery level, status, and current charge threshold."""
    bat = get_battery_path()
    if not bat:
        return None, None, None
    try:
        level = int((bat / "capacity").read_text().strip())
        status = (bat / "status").read_text().strip()
        # Try both threshold file names used by different drivers
        threshold = None
        for name in ("charge_control_end_threshold", "charge_stop_threshold"):
            f = bat / name
            if f.exists():
                try:
                    threshold = int(f.read_text().strip())
                    break
                except Exception:
                    pass
        return level, status, threshold
    except Exception:
        return None, None, None


# ---------------------------------------------------------------------------
# Auto-mode scheduling helpers
# ---------------------------------------------------------------------------

def _turning_points(curve):
    """Return a list of (hour, kind) for every local extremum on the circular 24-h curve.

    kind is 'max' or 'min'.  Flat plateaus are represented by the first hour
    of the plateau only (the rest are skipped so they don't produce a spurious
    second turning point of the same kind).
    """
    points = []
    prev = curve[23]
    for h in range(24):
        cur  = curve[h]
        nxt  = curve[(h + 1) % 24]
        # skip the interior of a plateau
        if cur == prev:
            prev = cur
            continue
        if cur >= prev and cur >= nxt:
            points.append((h, 'max'))
        elif cur <= prev and cur <= nxt:
            points.append((h, 'min'))
        prev = cur
    return points


def _build_schedule(curve, min_charge, max_charge, margin=2):
    """Compute the battery target for every hour of the day.

    For each hour the algorithm looks forward on the circular 24-h demand
    curve for the next local extremum:

    - Next turning point is a **maximum** and the potential rise
      (peak_value − current_demand) > margin:
        → target = max_charge  (demand is climbing; charge now to be full at peak)

    - Next turning point is a **minimum** and the potential fall
      (current_demand − valley_value) > margin:
        → target = min_charge  (demand is falling; wait — a cheaper slot is ahead,
          or we are past a peak and should keep discharging while demand is still
          elevated)

    - Potential ≤ margin (near-flat segment):
        → inherit the target of the previous hour (deadband — not worth acting
          on a negligible slope)

    The schedule is computed for all 24 hours in a single pass so that
    "inherit from previous" is unambiguous.  The first hour's default (when
    the very first hour falls in the deadband) is derived by scanning
    backwards to find the last decisive hour, which ensures consistency on
    a circular curve.

    margin=2 (demand-curve units, 0-100 scale) is hardcoded.  It equals
    ~5 % of the typical Central-European peak-to-trough swing and produces
    sensible deadbands for all 15 bundled country profiles without needing
    per-curve tuning.
    """
    tps = _turning_points(curve)
    if not tps:
        # Perfectly flat curve — nothing to do, stay at max
        return {h: max_charge for h in range(24)}

    # For each hour find the next turning point (circularly)
    # by pre-building an offset lookup: next_tp[h] = (tp_value, tp_kind)
    next_tp = {}
    for h in range(24):
        for offset in range(1, 25):
            candidate_h = (h + offset) % 24
            for tp_h, tp_kind in tps:
                if tp_h == candidate_h:
                    next_tp[h] = (curve[tp_h], tp_kind)
                    break
            if h in next_tp:
                break

    # First pass: compute decisive targets (ignoring deadband)
    decisive = {}
    for h in range(24):
        tp_val, tp_kind = next_tp[h]
        demand = curve[h]
        if tp_kind == 'max':
            potential = tp_val - demand
            if potential > margin:
                decisive[h] = max_charge
        else:  # 'min'
            potential = demand - tp_val
            if potential > margin:
                decisive[h] = min_charge

    # Determine the default target for hours that fall in the deadband at the
    # very start (h=0).  Scan backwards from h=23 to find the last decisive hour.
    default_target = max_charge  # safe fallback
    for h in range(23, -1, -1):
        if h in decisive:
            default_target = decisive[h]
            break

    # Second pass: fill in deadband hours by inheriting from the previous hour
    schedule = {}
    last = default_target
    for h in range(24):
        if h in decisive:
            last = decisive[h]
        schedule[h] = last
    return schedule


def calculate_target(hour, min_charge, max_charge, curve):
    """Return the battery target level for the given hour.

    Delegates to _build_schedule() which computes the full 24-h schedule
    based on the slope of the demand curve relative to upcoming turning points.
    """
    schedule = _build_schedule(curve, min_charge, max_charge)
    return schedule[hour]


def _next_change(hour, schedule):
    """Return (change_hour, new_target) for the next schedule transition after `hour`.

    Searches forward circularly up to 23 hours.  Returns (None, None) when the
    schedule is constant for the full day (no transition found).
    """
    current = schedule[hour]
    for offset in range(1, 24):
        h = (hour + offset) % 24
        if schedule[h] != current:
            return h, schedule[h]
    return None, None


# ---------------------------------------------------------------------------
# Manual-mode checkpoint helpers
# ---------------------------------------------------------------------------

def _parse_checkpoint_time(time_str):
    """Parse 'HH:MM' string into (hour, minute) integers, or raise ValueError."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: {time_str!r}")
    return h, m


def _checkpoints_sorted(checkpoints):
    """Return checkpoints sorted by time (HH:MM ascending).

    Each checkpoint is a dict: {"time": "HH:MM", "target": int}.
    Invalid entries are silently skipped.
    """
    valid = []
    for cp in checkpoints:
        try:
            h, m = _parse_checkpoint_time(cp["time"])
            valid.append((h * 60 + m, cp))
        except (KeyError, ValueError):
            pass
    valid.sort(key=lambda x: x[0])
    return [cp for _, cp in valid]


def get_manual_target(checkpoints, now=None):
    """Return (target, current_cp, next_cp) for the current time in manual mode.

    - target:      int charge % the battery should be held at right now.
    - current_cp:  the checkpoint currently in effect (dict), or None if no
                   checkpoints are defined.
    - next_cp:     the next upcoming checkpoint (dict), or None if there is none
                   (wraps circularly to the first checkpoint of the next day).

    The checkpoint in effect is the most recent one whose time ≤ now.
    If the current time is before the very first checkpoint of the day, the
    last checkpoint of the previous day (i.e. the last entry) applies.
    """
    sorted_cps = _checkpoints_sorted(checkpoints)
    if not sorted_cps:
        return None, None, None

    if now is None:
        now = datetime.now()
    now_minutes = now.hour * 60 + now.minute

    current_cp = None
    next_cp = None

    # Find the last checkpoint whose time <= now
    for i, cp in enumerate(sorted_cps):
        h, m = _parse_checkpoint_time(cp["time"])
        cp_minutes = h * 60 + m
        if cp_minutes <= now_minutes:
            current_cp = cp
            # next checkpoint: the one after this, or wrap to the first
            if i + 1 < len(sorted_cps):
                next_cp = sorted_cps[i + 1]
            else:
                next_cp = sorted_cps[0]  # wraps to tomorrow's first checkpoint

    if current_cp is None:
        # Current time is before the first checkpoint today → use the last one
        # (which carried over from yesterday)
        current_cp = sorted_cps[-1]
        next_cp = sorted_cps[0]

    return current_cp["target"], current_cp, next_cp


def _manual_status_text(checkpoints, now=None):
    """Build the tray status string for manual mode."""
    if not checkpoints:
        return "Manual mode: no checkpoints configured"

    target, current_cp, next_cp = get_manual_target(checkpoints, now)

    if current_cp is None:
        return "Manual mode: no checkpoints configured"

    if next_cp is not None and next_cp["time"] != current_cp["time"]:
        next_str = f" · next: {next_cp['target']}% at {next_cp['time']}"
    else:
        next_str = ""

    return f"Manual: hold {target}% until {_next_time_str(current_cp, next_cp)}{next_str}"


def _next_time_str(current_cp, next_cp):
    """Return the 'until HH:MM' part, or 'midnight' when wrapping."""
    if next_cp is None:
        return "midnight"
    # If next_cp is the same time as current_cp, single checkpoint — hold all day
    if next_cp["time"] == current_cp["time"]:
        return "end of day"
    return next_cp["time"]


# ---------------------------------------------------------------------------
# Main application class
# ---------------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

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

        # --- Manual mode section ---
        self.manual_toggle_item = Gtk.CheckMenuItem(label="Manual mode")
        self.manual_toggle_item.set_active(self.config.get("manual_mode", False))
        self.manual_toggle_item.connect("toggled", self._on_manual_toggle)
        menu.append(self.manual_toggle_item)

        manual_settings_item = Gtk.MenuItem(label="Manual mode settings…")
        manual_settings_item.connect("activate", self._on_manual_settings)
        menu.append(manual_settings_item)

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

    # ------------------------------------------------------------------
    # Timer / update loop
    # ------------------------------------------------------------------

    def _tick(self):
        """Called every minute. Handles discharge monitoring and UI refresh."""
        self._update()
        return GLib.SOURCE_CONTINUE

    def _update(self):
        hour = datetime.now().hour
        now  = datetime.now()

        if self.force_full:
            target = 100
            status_text = "⚡ Force charging to 100%"
            icon_name = "battery-full-charging"

        elif self.config.get("manual_mode", False):
            # --- Manual mode ---
            checkpoints = self.config.get("checkpoints", [])
            manual_target, current_cp, next_cp = get_manual_target(checkpoints, now)

            if manual_target is None:
                # No checkpoints defined — fall back to max_charge and warn
                target = self.config["max_charge"]
                status_text = "Manual mode: no checkpoints configured"
            else:
                target = manual_target
                status_text = _manual_status_text(checkpoints, now)

            if target >= 90:
                icon_name = "battery-full-charging"
            elif target >= 70:
                icon_name = "battery-good-charging"
            else:
                icon_name = "battery-low-charging"

        else:
            # --- Auto mode (demand-curve schedule) ---
            curve = self.curves.get(self.config["country"], DEFAULT_CURVE)

            if isinstance(list(curve.keys())[0], str):
                curve = {int(k): v for k, v in curve.items()}

            schedule = _build_schedule(curve, self.config["min_charge"], self.config["max_charge"])
            target = schedule[hour]
            demand = curve.get(hour, 50)
            phase = "discharging ↓" if target == self.config["min_charge"] else "charging ↑"

            change_h, change_target = _next_change(hour, schedule)
            if change_h is not None:
                change_dir = "charge" if change_target == self.config["max_charge"] else "discharge"
                next_str = f" · next: {change_dir} at {change_h:02d}:00"
            else:
                next_str = ""

            status_text = f"Grid demand: {demand}% → {phase} to {target}%{next_str}"

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
        bat = get_battery_path()
        if level is not None and bat is not None:
            behaviour_file = bat / "charge_behaviour"
            if behaviour_file.exists():
                if level > target:
                    set_charge_behaviour("force-discharge")
                else:
                    set_charge_behaviour("auto")

        # Update battery info label
        if level is not None:
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

    # ------------------------------------------------------------------
    # Menu action handlers
    # ------------------------------------------------------------------

    def _on_force_full(self, widget):
        self.force_full = not self.force_full
        self.force_item.set_label("✓ Charging to 100% (click to cancel)" if self.force_full else "⚡ Charge to 100%")
        self._update()

    def _on_manual_toggle(self, widget):
        """Toggle manual mode on/off from the CheckMenuItem."""
        self.config["manual_mode"] = widget.get_active()
        save_config(self.config)
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

    # ------------------------------------------------------------------
    # Manual-mode settings dialog
    # ------------------------------------------------------------------

    def _on_manual_settings(self, widget):
        """Open the manual-mode checkpoint editor dialog."""
        dialog = Gtk.Dialog(title="Manual Mode Settings", flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dialog.set_default_size(420, 360)

        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(12)
        box.set_margin_bottom(8)

        # --- Explanation label ---
        info_label = Gtk.Label()
        info_label.set_markup(
            "<small>Each checkpoint defines a time (HH:MM) and a battery target %.\n"
            "The battery is held at the checkpoint's target until the next checkpoint's time.\n"
            "Checkpoints wrap around midnight.</small>"
        )
        info_label.set_line_wrap(True)
        info_label.set_xalign(0)
        box.pack_start(info_label, False, False, 0)

        # --- ListStore: columns = [time_str, target_int] ---
        # We store target as string in the model for easier inline editing.
        store = Gtk.ListStore(str, int)  # (time "HH:MM", target %)

        for cp in _checkpoints_sorted(self.config.get("checkpoints", [])):
            store.append([cp["time"], cp["target"]])

        # --- TreeView ---
        tree = Gtk.TreeView(model=store)
        tree.set_reorderable(True)

        # Time column — editable text
        time_renderer = Gtk.CellRendererText()
        time_renderer.set_property("editable", True)
        time_renderer.connect("edited", self._on_checkpoint_time_edited, store)
        col_time = Gtk.TreeViewColumn("Time (HH:MM)", time_renderer, text=0)
        col_time.set_min_width(120)
        tree.append_column(col_time)

        # Target % column — editable spin
        target_renderer = Gtk.CellRendererSpin()
        adjustment = Gtk.Adjustment(value=80, lower=20, upper=100, step_increment=5)
        target_renderer.set_property("adjustment", adjustment)
        target_renderer.set_property("editable", True)
        target_renderer.connect("edited", self._on_checkpoint_target_edited, store)
        col_target = Gtk.TreeViewColumn("Target %", target_renderer, text=1)
        col_target.set_min_width(90)
        tree.append_column(col_target)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(160)
        scrolled.add(tree)
        box.pack_start(scrolled, True, True, 0)

        # --- Add / Remove buttons ---
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        add_btn = Gtk.Button(label="+ Add checkpoint")
        add_btn.connect("clicked", self._on_checkpoint_add, store)
        btn_box.pack_start(add_btn, False, False, 0)

        remove_btn = Gtk.Button(label="− Remove selected")
        remove_btn.connect("clicked", self._on_checkpoint_remove, store, tree)
        btn_box.pack_start(remove_btn, False, False, 0)

        box.pack_start(btn_box, False, False, 0)

        # --- Error / warning label (shown on validation failure) ---
        self._cp_error_label = Gtk.Label(label="")
        self._cp_error_label.set_xalign(0)
        self._cp_error_label.set_markup("")
        box.pack_start(self._cp_error_label, False, False, 0)

        dialog.show_all()

        if dialog.run() == Gtk.ResponseType.OK:
            # Collect checkpoints from the store and validate
            new_checkpoints = []
            error = None
            for row in store:
                time_str = row[0]
                target   = row[1]
                try:
                    _parse_checkpoint_time(time_str)
                except ValueError as e:
                    error = str(e)
                    break
                if not (20 <= target <= 100):
                    error = f"Target {target}% out of range (20–100)"
                    break
                new_checkpoints.append({"time": time_str, "target": target})

            if error:
                self._show_error_dialog(dialog, f"Invalid checkpoint: {error}")
            else:
                self.config["checkpoints"] = _checkpoints_sorted(new_checkpoints)
                save_config(self.config)
                self._update()

        dialog.destroy()

    def _on_checkpoint_time_edited(self, renderer, path, new_text, store):
        """Validate and apply an edited time cell."""
        try:
            h, m = _parse_checkpoint_time(new_text)
            store[path][0] = f"{h:02d}:{m:02d}"
        except ValueError:
            pass  # leave cell unchanged; user will see the error on OK

    def _on_checkpoint_target_edited(self, renderer, path, new_text, store):
        """Apply an edited target % cell."""
        try:
            val = int(float(new_text))
            val = max(20, min(100, val))
            store[path][1] = val
        except (ValueError, TypeError):
            pass

    def _on_checkpoint_add(self, button, store):
        """Append a new default checkpoint to the list."""
        store.append(["08:00", 80])

    def _on_checkpoint_remove(self, button, store, tree):
        """Remove the currently selected checkpoint row."""
        selection = tree.get_selection()
        model, it = selection.get_selected()
        if it is not None:
            store.remove(it)

    def _show_error_dialog(self, parent, message):
        err = Gtk.MessageDialog(
            transient_for=parent,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message,
        )
        err.run()
        err.destroy()

    # ------------------------------------------------------------------
    # Cleanup / quit
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Ensure force-discharge is never left active when the app exits."""
        set_charge_behaviour("auto")

    def _on_quit(self, widget=None):
        self._cleanup()
        Gtk.main_quit()

    def run(self):
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
