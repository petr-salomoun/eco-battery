Name:           eco-battery
Version:        1.0.1
Release:        1%{?dist}
Summary:        Smart battery charging based on grid demand

License:        MIT
URL:            https://github.com/yourusername/eco-battery
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       libappindicator-gtk3

%description
eco-battery automatically adjusts ThinkPad battery charging thresholds
inversely to electricity grid demand patterns. Charge more during off-peak,
less during peak hours - helping balance the power grid.

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
* Mon Mar 02 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.0.1-1
- Renamed project from eco-battery to eco-battery
- Fix settings persistence: config directory updated to ~/.config/eco-battery

* Sat Feb 28 2026 Petr Salomoun <petr.salomoun@gmail.com> - 1.0.0-1
- Initial release
