# eco-battery

Smart battery charging that follows grid demand patterns. Your laptop helps balance the power network automatically.

## How it works

- **Low grid demand** (night) → Charge to maximum (e.g., 100%)
- **High grid demand** (evening peak) → Charge only to minimum (e.g., 40%)
- **Everything else** → Proportionally in between

No configuration needed. Install and forget.

## Install

### Debian/Ubuntu
```bash
sudo dpkg -i eco-battery_1.0.1-1_all.deb
sudo apt-get install -f  # Install dependencies if needed
```

### Fedora/RHEL
```bash
sudo dnf install eco-battery-1.0.1-1.noarch.rpm
```

### Manual
```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1  # Debian
# or
sudo dnf install python3-gobject gtk3 libappindicator-gtk3  # Fedora

sudo cp eco_battery.py /usr/bin/eco-battery
sudo chmod +x /usr/bin/eco-battery
sudo cp data/curves.json /usr/share/eco-battery/curves.json
sudo cp 99-eco-battery.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Usage

The app starts automatically on login. Look for the battery icon in your system tray.

**Menu options:**
- View current grid demand and charge threshold
- **⚡ Charge to 100%** - One click when you need full battery
- **Settings** - Adjust min/max charge levels (default: 40-100%)

## Settings

Click ⚙️ Settings in the tray menu to configure:
- **Minimum charge**: Lowest battery level during peak demand (default: 40%)
- **Maximum charge**: Full charge during low demand (default: 100%)
- **Demand curve**: Country-specific grid pattern (CZ, DE, PL, AT, SK, or default)

Settings are saved to `~/.config/eco-battery/config.json` and persist across restarts.

## Requirements

- ThinkPad with `thinkpad_acpi` kernel module
- Linux with GTK3 and AppIndicator support

## Building packages

```bash
# Debian
./build-deb.sh

# RPM
./build-rpm.sh
```

## License

MIT
