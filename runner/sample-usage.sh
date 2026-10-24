#!/bin/sh
# Appends this minute's line of what the machine has free to ~/.local/state/ci-runner/usage.log
# (usage.py says what each field is). Run every minute by example-app-runner-usage.timer.
set -eu
here=$(dirname "$0")
. "$here/settings.sh"
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
exec python3 "$here/usage.py" sample "$machine"
