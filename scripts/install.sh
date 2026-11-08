#!/bin/sh
# Install the selected package, then provision the local homes.
set -eu
source=${1:-git+https://github.com/NatChung/orchd.git}
uv tool install "$source"
orchd install
