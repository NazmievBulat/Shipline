#!/bin/sh
# Lends one of main's idle runners to PR jobs, and gives the merge train's first member a runner
# first (lend_runners.py says how). Runs on the machine with main's runners, as the runner
# programs' own GitHub identity, or without one as the person who is logged in to GitHub with `gh`.
set -eu
here=$(dirname "$0")
. "$here/settings.sh"
# Main's runners of this machine are named home-main and home-main-2 (keep-runner.sh).
exec python3 "$here/lend_runners.py" "$repository" "$label" "$first_label" "$main_label" \
  "$main_lent" "$lend_poll_seconds" home-main "$scratch_prefix"
