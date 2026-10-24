#!/bin/sh
# Runs on the home machine, as the runner programs' own GitHub identity (github_identity.py), or
# without one as the person who is logged in to GitHub with `gh`. It keeps one runner
# waiting for work: each time round it asks GitHub for a registration token (good for one hour),
# starts a fresh container that takes one job, and starts another when that container is gone. So
# every job gets an empty box, and nothing a job leaves behind reaches the next one.
#
#   keep-runner.sh <number>            an ordinary runner
#   keep-runner.sh <number> docker     a runner whose job has Docker, inside a virtual machine
set -u

number=$1
kind=${2:-ordinary}
here=$(dirname "$0")
. "$here/settings.sh"
# The machine's name, which starts the runner's: install.sh writes it.
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
. "$here/runner-container.sh"

if [ "$kind" = planner ]; then
  name=example-app-runner-planner
  runner_name=$machine-planner
  runner_label=$planner_label
  place=$planner_place
elif [ "$kind" = main ]; then
  name=example-app-runner-main
  runner_name=$machine-main
  runner_label=$main_label
  place=$((main_place - (number > 1 ? number - 1 : 0)))
  if [ "$number" -gt 1 ]; then
    name="$name-$number"
    runner_name="$runner_name-$number"
  fi
elif [ "$kind" = docker ]; then
  name="example-app-runner-docker-$number"
  runner_name="$machine-docker-$number"
  runner_label=$docker_label
  place=$((most_runners + 1 - number))
else
  name="example-app-runner-$number"
  runner_name="$machine-$number"
  runner_label=$standby_label
  place=$number
fi

# A Docker-capable runner takes no job from an image that check-docker-runner.sh has not passed.
# The weekly rebuild brings a new kernel, QEMU and Docker, so the check is run again for every new
# image; the image that passed is written beside this script. Two runners do not check at once,
# and the one that waited for the other reads the file again: the image it waited for has passed.
docker_image_is_checked() {
  [ "$kind" = docker ] || return 0
  built=$(docker image inspect --format '{{.Id}}' "$docker_image") || return 1
  [ "$(cat "$here/checked-docker-image" 2>/dev/null)" != "$built" ] || return 0
  (
    flock 9
    [ "$(cat "$here/checked-docker-image" 2>/dev/null)" != "$built" ] || exit 0
    "$here/check-docker-runner.sh" || exit 1
    printf '%s\n' "$built" >"$here/checked-docker-image"
  ) 9<"$here/settings.sh"
}

while true; do
  python3 "$here/github_budget.py" wait --identity runner keep-runner
  started=$(date +%s)
  succeeded=0
  # No firewall, no runner: a job must not be able to reach this machine or the home network.
  # RUNNER_WITHOUT_FIREWALL=1 skips both checks, for a first trial.
  if ! network_at "$place"; then
    echo "Could not make the network $network-$place. Not starting a runner."
  elif [ "${RUNNER_WITHOUT_FIREWALL:-}" != 1 ] && ! firewall_is_in_place "$network-$place"; then
    echo "The firewall for CI containers is not in place (see README.md). Not starting a runner."
  elif [ "${RUNNER_WITHOUT_FIREWALL:-}" != 1 ] && ! docker_image_is_checked; then
    echo "check-docker-runner.sh did not pass for this image (see README.md). Not starting a runner."
  elif token=$(python3 "$here/github_budget.py" call --identity runner keep-runner api -X POST "repos/$repository/actions/runners/registration-token" --jq .token); then
    printf '%s\n' "$token" | start_container "$kind" "$name" "$network-$place" \
      --env RUNNER_REPOSITORY="$repository" \
      --env RUNNER_NAME="$runner_name" \
      --env RUNNER_LABEL="$runner_label" && succeeded=1
  fi
  # A successful ordinary runner may have run a six-second routing job: replace it at once.
  # QEMU exits successfully even when registration in the guest failed, so keep its short-round
  # backoff. Failed short starts wait too, so a fault does not become a tight loop of requests.
  if { [ "$kind" = docker ] || [ "$succeeded" -eq 0 ]; } &&
    [ $(($(date +%s) - started)) -lt 30 ]; then
    sleep "$(python3 "$here/github_budget.py" jitter 60)"
  fi
done
