#!/bin/sh
# Builds the runner images, <prefix>-runner and <prefix>-runner-docker, with the repository's own
# command (`runner.image_build`), on main's files (main-command.sh). install.sh runs it, and so
# does <prefix>-runner-image.timer every week: GitHub refuses runners that fall far behind its
# own version.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/settings.sh"
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
# install.sh names the prefix before it writes pool-settings.sh.
prefix=${RUNNER_PREFIX:-${prefix:-}}
. "$here/names.sh"
. "$here/main-command.sh"
on_main image_build
