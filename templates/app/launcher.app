#!/bin/sh
# Launcher entry: the firmware lists *.app files in applications/.
# FAT storage does not keep execute bits, so set them before starting.
BIN="$(dirname "$0")/@APP@/@APP@"
chmod +x "$BIN" 2>/dev/null
cd "$(dirname "$BIN")" || exit 1
exec "$BIN"
