#!/usr/bin/env bash
# Builds the runner images. The install runs it, and so does `<prefix>-runner-image.timer` every
# week: GitHub refuses runners that fall far behind its own version.
#
#   RUNNER_PREFIX=example-app build-images.sh
#
# Run from the repository's main checkout. Only main, as last fetched (`origin/main`), counts:
# its files are taken into a folder of their own, so a branch the checkout holds, a fork's pull
# request among them, never runs here with the runner user's Docker rights. When main's
# shipline.json names `runner.image_build`, that command line builds the images, in that folder,
# with RUNNER_PREFIX set. Else the pool builds its own: the ordinary image `<prefix>-runner` from
# the Dockerfile beside this file, then, only on a machine with /dev/kvm (RUNNER_KVM names
# another path), the Docker-capable `<prefix>-runner-docker` from `vm/`. `--pull` takes each base
# image anew.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

prefix=${RUNNER_PREFIX:-}
if [[ ! $prefix =~ ^[a-z]([a-z0-9-]{0,23}[a-z0-9])?$ ]]; then
  echo "RUNNER_PREFIX must be at most 25 lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit, not '$prefix'" >&2
  exit 1
fi

main=refs/remotes/origin/main
if ! git rev-parse --quiet --verify "$main^{commit}" >/dev/null; then
  echo "$PWD has no origin/main: run this from the repository's main checkout" >&2
  exit 1
fi
tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT
git archive --format=tar "$main" | tar -x -C "$tree"

command=""
if [[ -f $tree/shipline.json ]]; then
  command=$(jq -r '.runner.image_build // empty' "$tree/shipline.json")
fi
if [[ -n $command ]]; then
  cd "$tree"
  export RUNNER_PREFIX=$prefix
  bash -c "$command"
  exit 0
fi

docker build --pull -t "$prefix-runner" "$here"
if [[ -e ${RUNNER_KVM:-/dev/kvm} ]]; then
  docker build --pull -t "$prefix-runner-docker" "$here/vm"
fi
