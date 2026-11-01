#!/bin/sh
# pbtk probe launcher: the firmware lists *.app files in applications/.
# Run through sh so the FAT storage's missing execute bits do not matter.
exec /bin/sh "$(dirname "$0")/pbtk-probe/probe.sh"
