#!/usr/bin/env bash
# Builds the runner images. The install runs it, and so does `<prefix>-runner-image.timer` every
# week: GitHub refuses runners that fall far behind its own version.
#
#   RUNNER_PREFIX=example-app [SHIPLINE_RUNNER_IMAGE_BUILD=<command>] build-images.sh
#
# Run from the repository's main checkout. When the repository names its own command
# (`runner.image_build` of its shipline.json, given here as SHIPLINE_RUNNER_IMAGE_BUILD), that
# command builds them, from the checkout, with RUNNER_PREFIX set. Else the pool builds its own:
# the ordinary image `<prefix>-runner` from the Dockerfile beside this file, then, only on a
# machine with /dev/kvm, the Docker-capable `<prefix>-runner-docker` from `vm/`. `--pull` takes
# each base image anew.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

prefix=${RUNNER_PREFIX:-}
if [[ ! $prefix =~ ^[a-z]([a-z0-9-]{0,23}[a-z0-9])?$ ]]; then
  echo "RUNNER_PREFIX must be at most 25 lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit, not '$prefix'" >&2
  exit 1
fi

if [[ -n ${SHIPLINE_RUNNER_IMAGE_BUILD:-} ]]; then
  export RUNNER_PREFIX=$prefix
  exec bash -c "$SHIPLINE_RUNNER_IMAGE_BUILD"
fi

docker build --pull -t "$prefix-runner" "$here"
if [[ -e ${RUNNER_KVM:-/dev/kvm} ]]; then
  docker build --pull -t "$prefix-runner-docker" "$here/vm"
fi
