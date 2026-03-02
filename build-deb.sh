#!/bin/bash
set -e

# Clean any old compat file
rm -f debian/compat

# Build
dpkg-buildpackage -us -uc -b

echo "Package built: ../eco-battery_1.0.1-1_all.deb"
