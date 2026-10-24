#!/usr/bin/env bash
# Every name of the runner pool comes from one prefix, RUNNER_PREFIX (`runner.prefix` of the
# repository's shipline.json): its user, folders, images, network and units, and the base label,
# RUNNER_LABEL (`runner.label`), `<prefix>-home` when it names none.
#
#   RUNNER_PREFIX=example-app render-units.sh <folder>    the systemd units, into <folder>
#   RUNNER_PREFIX=example-app render-units.sh --names     the names, as `name=value` lines
#   RUNNER_PREFIX=example-app render-units.sh --labels    the labels every runner carries
#
# The units are the templates in `units/` beside this file, with `@PREFIX@` replaced by the
# prefix in their names and their text; nothing else in them varies. Machine names and counts
# are the machine's own (`~/.config/shipline/runner.sh`), never part of a unit.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  echo "usage: RUNNER_PREFIX=<prefix> [RUNNER_LABEL=<label>] render-units.sh <folder> | --names | --labels" >&2
  exit 2
}

# It names a unit and, as `<prefix>-runner`, a user of at most 32 characters; it is also put in
# a sed expression, so nothing else may pass.
prefix=${RUNNER_PREFIX:-}
if [[ ! $prefix =~ ^[a-z]([a-z0-9-]{0,23}[a-z0-9])?$ ]]; then
  echo "RUNNER_PREFIX must be at most 25 lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit, not '$prefix'" >&2
  exit 1
fi
label=${RUNNER_LABEL:-$prefix-home}
if [[ ! $label =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
  echo "RUNNER_LABEL must be lowercase letters, digits and hyphens, starting and ending with a letter or digit, not '$label'" >&2
  exit 1
fi

names() {
  cat <<NAMES
prefix=$prefix
user=$prefix-runner
programs=/opt/$prefix-runner
state=/var/lib/$prefix-runner
image=$prefix-runner
docker_image=$prefix-runner-docker
network=$prefix-runner
hub_cache=$prefix-runner-hub-cache
units=$prefix-runner
label=$label
standby_label=$label-standby
first_label=$label-first
main_label=$label-main
docker_label=$label-docker
NAMES
}

# Every machine's runners carry the same labels, so GitHub gives a job to whichever machine has
# one free: the base label, a spare's before it takes a slot, the merge train's first member's,
# main's runners' and the Docker runners' (`runner_labels` of workflow_config.py).
labels() {
  printf '%s\n' "$label" "$label-standby" "$label-first" "$label-main" "$label-docker"
}

render() {
  local folder=$1 template name
  mkdir -p "$folder"
  for template in "$here"/units/*; do
    name=$(basename "$template")
    sed "s/@PREFIX@/$prefix/g" "$template" >"$folder/${name//@PREFIX@/$prefix}"
    chmod 0644 "$folder/${name//@PREFIX@/$prefix}"
  done
}

case ${1:-} in
  --names) [[ $# -eq 1 ]] || usage; names ;;
  --labels) [[ $# -eq 1 ]] || usage; labels ;;
  "" | -*) usage ;;
  *) [[ $# -eq 1 ]] || usage; render "$1" ;;
esac
