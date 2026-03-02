#!/bin/bash
# Build RPM package
set -e

VERSION="1.0.1"

# Setup rpmbuild directories
mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# Create tarball
tar czf ~/rpmbuild/SOURCES/eco-battery-${VERSION}.tar.gz \
    --transform "s,^,eco-battery-${VERSION}/," \
    eco_battery.py data eco-battery.desktop eco-battery-autostart.desktop 99-eco-battery.rules

# Copy spec and build
cp eco-battery.spec ~/rpmbuild/SPECS/
rpmbuild -bb ~/rpmbuild/SPECS/eco-battery.spec

echo "Package built in ~/rpmbuild/RPMS/"
