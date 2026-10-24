#!/bin/sh
# One isolated, ephemeral planner slot; its counts are refreshed from the host.
set -eu
here=$(dirname "$0")
. "$here/settings.sh"
# install.sh records the ordinary job slots it was given; the spare beyond them is not a slot.
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
state=$HOME/.local/state/ci-overflow
mkdir -p "$state/home" "$state/ledger"
# The image's runner user must be able to commit reservations to the bind mount.
# Only this ledger is shared writable; host counts are mounted read-only.
chmod 777 "$state/ledger"
# The other machines' PR runners count too: a run waits for a free one there as for one here.
python3 "$here/planner-state.py" "$ordinary_jobs" "$ordinary_blacksmith_jobs" "$repository" \
  "$other_machines" "$label" &
publisher=$!
"$here/keep-runner.sh" 0 planner &
runner=$!
trap 'kill "$publisher" "$runner" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' INT TERM
wait "$runner"
