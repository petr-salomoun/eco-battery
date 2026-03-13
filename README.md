# eco-battery

Smart battery charging that follows electricity grid demand patterns — good for the grid, good for your battery.

## Why it matters

Laptop batteries degrade faster when repeatedly charged to 100%. At the same time, electricity grids
are under most stress during peak demand hours (typically early evening), when generation relies
heavily on expensive and polluting peaker plants.

eco-battery solves both problems at once:

- **At each demand valley** → charge to your maximum (e.g. 95%) — but only once the valley is reached, not prematurely while demand is still falling toward it
- **At each demand peak** → discharge to your minimum (e.g. 40%) — and keep discharging on the downslope while grid demand remains elevated
- **Near-flat segments** → hold the current target; too little to gain from acting

The scheduling algorithm looks at the **slope** of the 24 h demand curve at each
hour by finding the next local extremum ahead:

- If the next turning point is a **peak** and demand has room to rise significantly
  → charge now; the battery will be full when the peak arrives
- If the next turning point is a **valley** and demand still has room to fall
  → hold low; a cheaper charging slot is coming, or we are past a peak and
  should keep discharging while demand is still elevated
- If the next turning point is very close in value (within 2 demand units)
  → near-flat slope, not worth acting; hold the previous target

This means charging starts *at* the valley (not before it), discharging starts
*at* the peak and continues naturally through the descent, and each charge/discharge
event is precisely sized to the actual shape of the day's demand curve.
Double-hump profiles (e.g. FR, DE with morning and evening peaks) are handled
naturally: each peak gets its own valley-triggered charge window.

The battery spends less time at high state-of-charge, which is the primary
cause of lithium-ion degradation. eco-battery does this automatically, without
you having to think about it.

**Win-win: fewer CO₂ emissions from the grid, longer battery lifespan.**

## Hardware requirements and limitations

eco-battery controls the battery via the Linux kernel sysfs interface:

```
/sys/class/power_supply/BAT0/charge_control_end_threshold
```

This interface is **not available on all laptops**. It requires kernel driver support for your
specific battery/firmware combination. Known to work:

- **ThinkPad** laptops (via the `thinkpad_acpi` kernel module) — best supported
- **ASUS** laptops (via `asus-nb-wmi`)
- **Huawei** laptops (via `huawei-wmi`)
- Some **Dell**, **HP**, and **Toshiba** models with recent kernels

**Will not work on:**
- Most consumer laptops without vendor-specific kernel drivers
- Apple hardware
- Virtual machines

To check if your hardware is supported before installing:

```bash
ls /sys/class/power_supply/BAT*/charge_control_end_threshold
```

If the file exists, eco-battery will work. If not, the app will show an error dialog on startup
and exit — it cannot operate without this kernel interface.

**ThinkPad users:** if the file is missing, load the module first:

```bash
sudo modprobe thinkpad_acpi
```

## Install

### Debian/Ubuntu
```bash
sudo dpkg -i eco-battery_1.1.0-1_all.deb
sudo apt-get install -f  # install missing dependencies if needed
```

### Fedora/RHEL
```bash
sudo dnf install eco-battery-1.1.0-1.noarch.rpm
```

### Manual
```bash
# Install dependencies
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1  # Debian/Ubuntu
# or
sudo dnf install python3-gobject gtk3 libayatana-appindicator-gtk3  # Fedora

# Install files
sudo install -Dm755 eco_battery.py /usr/bin/eco-battery
sudo install -Dm644 data/curves.json /usr/share/eco-battery/curves.json
sudo install -Dm644 99-eco-battery.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Allow your user to write thresholds without sudo
sudo usermod -aG plugdev $USER
# Log out and back in for the group change to take effect
```

## Usage

The app starts automatically on login. Look for the battery icon in your system tray.

**Tray menu:**
- Current grid demand, current phase (charging/discharging), and next scheduled change time
- **⚡ Charge to 100%** — one-click override when you need a full battery
- **⚙️ Settings** — adjust thresholds and demand curve

## Settings

| Setting | Default | Description |
|---|---|---|
| Maximum charge | 95% | Charge limit during off-peak / valley hours |
| Minimum charge | 40% | Charge limit during peak demand hours |
| Demand curve | AT | Country/region grid profile |

Available demand curves: AT, AU, BR, CA, CZ, DE, ES, FR, GB, IN, IT, JP, PL, SK, US.

Settings are saved to `~/.config/eco-battery/config.json`.

## Building packages

```bash
# Debian/Ubuntu
./build-deb.sh

# RPM (Fedora/RHEL)
./build-rpm.sh
```

## License

MIT — © 2026 Petr Salomoun
