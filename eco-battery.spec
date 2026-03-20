Name:           eco-battery
Version:        1.2.0
Release:        1%{?dist}
Summary:        Smart battery charging based on grid demand

License:        MIT
URL:            https://github.com/petr-salomoun/eco-battery
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       libappindicator-gtk3

%description
eco-battery automatically adjusts laptop battery charge thresholds in inverse
proportion to electricity grid demand. It charges more during off-peak hours
(night) when electricity is cheap and clean, and limits charging during peak
demand (typically evening) when the grid is most stressed.

This is a win-win: reduced CO2 emissions from avoided peaker plant use, and
significantly extended battery lifespan - lithium-ion cells degrade much faster
when kept at high state-of-charge.

Requires kernel support for charge_control_end_threshold (e.g. ThinkPad via
thinkpad_acpi, ASUS via asus-nb-wmi, or other supported drivers). The app
detects unsupported hardware on startup and shows a clear error dialog.

%prep
%autosetup

%install
install -D -m 755 eco_battery.py %{buildroot}%{_bindir}/eco-battery
install -D -m 644 data/curves.json %{buildroot}%{_datadir}/eco-battery/curves.json
install -D -m 644 eco-battery.desktop %{buildroot}%{_datadir}/applications/eco-battery.desktop
install -D -m 644 eco-battery-autostart.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/eco-battery.desktop
install -D -m 644 99-eco-battery.rules %{buildroot}/usr/lib/udev/rules.d/99-eco-battery.rules

%post
udevadm control --reload-rules 2>/dev/null || :
udevadm trigger 2>/dev/null || :

%files
%{_bindir}/eco-battery
%{_datadir}/eco-battery/curves.json
%{_datadir}/applications/eco-battery.desktop
%{_sysconfdir}/xdg/autostart/eco-battery.desktop
/usr/lib/udev/rules.d/99-eco-battery.rules

%changelog
* Fri Mar 20 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.2.0-1
- Manual mode: user-defined daily checkpoints (HH:MM + target %) override
  the automatic demand-curve schedule
- Checkpoint editor dialog: inline-editable TreeView with add/remove buttons,
  drag-to-reorder, spin-based target % input, and HH:MM validation
- Manual mode toggle (CheckMenuItem) in tray menu; persisted in config.json
- Tray status line shows active checkpoint and next upcoming checkpoint
- Checkpoints sort by time and wrap circularly around midnight
- Backward-compatible config defaults (manual_mode=false, checkpoints=[])

* Wed Mar 11 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.1.0-1
- Slope-aware charge scheduling: charge at valley, discharge at peak and descent
- Tray status shows next scheduled behaviour change with absolute time
- Real country grid demand profiles for all 15 bundled countries
- Active discharge via charge_behaviour (force-discharge / auto)
- Default max charge lowered to 95%
- Safe exit: force-discharge cleared on quit or SIGTERM

* Mon Mar 02 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.0.1-1
- Renamed project from ecco-battery to eco-battery
- Fix settings persistence: config directory updated to ~/.config/eco-battery

* Sat Feb 28 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.0.0-1
- Initial release
