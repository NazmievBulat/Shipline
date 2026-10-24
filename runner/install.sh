#!/bin/sh
# Run once on each machine that runs runners, as yourself (not root), in the main checkout of the
# repository whose runners they are (or with that checkout named in SHIPLINE_REPO):
#
#   sh install.sh [--github-token <file> | --github-app <app id> <key file>]
#                 [how many PR job slots] [how many with Docker]
#
# The repository's shipline.json names the pool (`repository`, `runner.prefix`, `runner.label`,
# `runner.planner_label`) and its own commands that build the images and run the Docker Hub cache
# (`runner.image_build`, `runner.hub_cache`). What is the machine's own is in
# ~/.config/shipline/runner.sh, or the file SHIPLINE_RUNNER_SETTINGS names, outside any
# repository: its name, the other machines' names, what it runs (its roles) and how many runners
# of each kind it gets when no count is given here (README.md). On a machine with the role `main`
# two main runners are added, and with the role `ordinary` one PR spare.
#
# --github-token and --github-app give the runner programs a GitHub identity of their own
# (github_identity.py): the file is checked against GitHub and copied to the folder
# `runner.identity_folder` names (~/.config/shipline-runner by default), for this user alone, and
# never printed. Once given, a later install keeps it. Without one they use your gh login.
#
# It copies these files out of the checkout, renders the units for the prefix (render-units.sh),
# builds the images and starts the runners as services of your user. The copy matters: what runs
# on this machine with your GitHub login must not change because a pull request changed a file.
# To take a newer version, run this again from main.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
units=$HOME/.config/systemd/user
. "$here/settings.sh"

github_token=
github_app_id=
github_app_key=
while [ $# -gt 0 ]; do
  case "$1" in
    --github-token)
      [ $# -ge 2 ] || { echo "--github-token needs the file that holds the token." >&2; exit 1; }
      github_token=$2
      shift 2
      ;;
    --github-app)
      [ $# -ge 3 ] || {
        echo "--github-app needs the app's ID and the file of its private key." >&2
        exit 1
      }
      github_app_id=$2
      github_app_key=$3
      shift 3
      ;;
    *) break ;;
  esac
done
if [ -n "$github_token" ] && [ -n "$github_app_id" ]; then
  echo "Give either --github-token or --github-app, not both." >&2
  exit 1
fi

# The repository's own values, from the selected config, each quoted for sh.
config=$(python3 "$here/../scripts/workflow/workflow_config.py" sh) || exit 1
eval "$config"
config_file=${SHIPLINE_CONFIG:-$SHIPLINE_REPO/shipline.json}
# A checkout config names a pool that runs main's commands, so it must be main's file too.
# An external installation config has no counterpart on origin/main.
if [ "$SHIPLINE_CONFIG" = "$SHIPLINE_REPO/shipline.json" ]; then
  if ! git -C "$SHIPLINE_REPO" show refs/remotes/origin/main:shipline.json 2>/dev/null |
    cmp -s - "$SHIPLINE_CONFIG"; then
    echo "$SHIPLINE_CONFIG is not the one of origin/main: run this from main, as last fetched." >&2
    exit 1
  fi
elif [ -n "$SHIPLINE_CONFIG" ]; then
  printf 'Read runner configuration from %s.\n' "$SHIPLINE_CONFIG"
fi
if [ -z "$SHIPLINE_REPOSITORY" ] || [ -z "$SHIPLINE_RUNNER_PREFIX" ] ||
  [ -z "$SHIPLINE_RUNNER_IMAGE_BUILD" ]; then
  echo "$config_file must name the repository, runner.prefix and runner.image_build (runner/README.md)." >&2
  exit 1
fi

# The machine's own: its name, the other machines', its roles and counts.
machine=
roles=
other_machines=
docker_runners=0
machine_settings=${SHIPLINE_RUNNER_SETTINGS:-$HOME/.config/shipline/runner.sh}
if [ ! -f "$machine_settings" ]; then
  echo "This machine has no runner settings: write $machine_settings (runner/README.md)." >&2
  exit 1
fi
. "$machine_settings"
# The repository's names are its file's, whatever the machine's file says.
repository=$SHIPLINE_REPOSITORY
prefix=$SHIPLINE_RUNNER_PREFIX
label=$SHIPLINE_RUNNER_LABEL
planner_label=$SHIPLINE_RUNNER_PLANNER_LABEL
checkout=$SHIPLINE_REPO
installed=$HOME/.local/share/$prefix-runner
# A name is one word of small letters and digits: it starts the runners' names (names.sh), and
# the other machines' names go into a pattern.
for name in "$machine" $other_machines; do
  case "$name" in
    '' | *[!a-z0-9]*)
      echo "$machine_settings: '$name' is no machine name: one word of small letters and digits." >&2
      exit 1
      ;;
  esac
done
if [ -z "$roles" ]; then
  echo "$machine_settings names no roles: what this machine runs (runner/README.md)." >&2
  exit 1
fi
recorded=
recorded_jobs=
if [ -f "$installed/pool-settings.sh" ]; then
  recorded=$(
    machine=
    . "$installed/pool-settings.sh"
    printf '%s' "$machine"
  )
  # The PR slots this machine's own settings gave at its last install. The balancer changes
  # ordinary_jobs as it runs (balance_runners.py), which is no move; a record written before
  # installed_slots has only that.
  recorded_jobs=$(
    ordinary_jobs=
    installed_slots=
    . "$installed/pool-settings.sh"
    printf '%s' "${installed_slots:-$ordinary_jobs}"
  )
  # A count that is no number is no record of one.
  case "$recorded_jobs" in '' | *[!0-9]*) recorded_jobs= ;; esac
fi
# Two machines with the same name would take each other's runner registrations (`--replace`) and
# retire each other's runners, so a machine is never taken to be one it was not named.
if [ -n "$recorded" ] && [ "$machine" != "$recorded" ]; then
  echo "This machine's runners were installed as $recorded, not $machine. To rename it, stop its runners and remove $installed first." >&2
  exit 1
fi
. "$here/names.sh"
has_role() {
  case " $roles " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}
# Each job's virtual machine takes what settings.sh gives it, so two take twice that.
runners=${1:-$ordinary_jobs}
docker_runners=${2:-$docker_runners}
has_role ordinary || [ "${1:-0}" = 0 ] || {
  echo "$machine runs no PR jobs (roles: $roles), so it takes no count of them." >&2
  exit 1
}
has_role docker || [ "${2:-0}" = 0 ] || {
  echo "$machine runs no Docker runners (roles: $roles), so it takes no count of them." >&2
  exit 1
}
has_role ordinary || runners=0
has_role docker || docker_runners=0
spares=0
! has_role ordinary || spares=$ordinary_spares

if ! [ "$runners" -ge 0 ] 2>/dev/null || ! [ "$docker_runners" -ge 0 ] 2>/dev/null ||
  [ $((runners + spares + docker_runners)) -gt "$most_runners" ]; then
  echo "There must be at least 1 ordinary runner, and at most $most_runners runners of both kinds together (including the spare)." >&2
  exit 1
fi
# A machine of a pool of several may give all its PR slots to the others (README.md, "Moving a
# machine's PR slots"): its spare still takes one while no other machine has an online ordinary
# runner (keep-spare.sh), so it needs the spare. The only machine of the pool keeps one slot.
if has_role ordinary && [ "$runners" -lt 1 ] &&
  { [ -z "$other_machines" ] || [ "$spares" -lt 1 ]; }; then
  echo "There must be at least 1 ordinary runner: $machine may have no PR slot of its own only while other_machines names another machine to take its PR jobs and it keeps its spare (ordinary_spares)." >&2
  exit 1
fi
# The machine's own cap of one ordinary job, in place of settings.sh's `memory`: whole gigabytes,
# as Docker reads them (runner-container.sh). Below a gigabyte no job would start.
size=
case "$job_memory" in
  [1-9]*g) size=${job_memory%g} ;;
esac
if [ -n "$job_memory" ] && case "$size" in '' | *[!0-9]*) true ;; *) false ;; esac; then
  echo "$machine_settings: job_memory must be whole gigabytes, such as 6g, not '$job_memory'." >&2
  exit 1
fi
ordinary_total=$((runners + spares))
# A machine that gives up no PR slot while Blacksmith has budget keeps all of them.
[ -n "${ordinary_blacksmith_jobs:-}" ] || ordinary_blacksmith_jobs=$runners
# Every count is a number, nothing more: pool-settings.sh holds them as they are.
for count in "$runners" "$docker_runners" "$spares" "$ordinary_blacksmith_jobs"; do
  case "$count" in
    '' | *[!0-9]*)
      echo "$machine_settings: '$count' is no count." >&2
      exit 1
      ;;
  esac
done
[ "$ordinary_blacksmith_jobs" -le "$runners" ] || ordinary_blacksmith_jobs=$runners
# The job of a Docker-capable runner is inside a virtual machine, which needs this device.
# RUNNER_KVM_DEVICE names another path for it, for the tests of this script.
kvm=${RUNNER_KVM_DEVICE:-/dev/kvm}
if [ "$docker_runners" -gt 0 ] && ! [ -e "$kvm" ]; then
  echo "This machine cannot run virtual machines (no /dev/kvm), so it gets no runner with Docker." >&2
  docker_runners=0
fi
# The Docker Hub cache is the repository's (`runner.hub_cache`), and only for Docker runners.
hub_cache=
if [ -n "$SHIPLINE_RUNNER_HUB_CACHE" ] && has_role docker; then
  hub_cache=yes
fi

# The GitHub programs as the installed runner programs run them: a copy with no config reader
# beside it, which reads the identity's folder, `runner.identity_folder`, from the file
# identity-folder beside it (github_identity.py). Gone at the end.
tools=$(mktemp -d)
trap 'rm -rf "$tools"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -L "$here/github_identity.py" "$here/github_budget.py" "$tools/"
printf '%s\n' "$SHIPLINE_IDENTITY_FOLDER" >"$tools/identity-folder"
# The units for this prefix, rendered from systemd/.
RUNNER_PREFIX=$prefix bash "$here/render-units.sh" "$tools/units"

# Before anything is stopped or copied, and before the move's checks, which read GitHub's runners
# with it: github_identity.py checks the new identity against GitHub before it replaces the old
# one, so a refused identity leaves the machine as it was, and a refused move afterwards changes
# no more than an identity GitHub has just accepted. The services running now take the new one at
# their next call.
if [ -n "$github_token" ]; then
  python3 "$tools/github_identity.py" install --repository "$repository" --token "$github_token"
elif [ -n "$github_app_id" ]; then
  python3 "$tools/github_identity.py" install --repository "$repository" \
    --app "$github_app_id" "$github_app_key"
fi
# This script's own calls are the runner programs' too.
gh() {
  python3 "$tools/github_budget.py" wait --identity runner runner-install &&
    python3 "$tools/github_budget.py" call --identity runner runner-install "$@"
}

# A move of PR slots is both machines agreeing (README.md, "Moving a machine's PR slots"): the
# receiving machine installs first, and only then this one gives its slots up. So before anything
# is stopped, a machine of a pool that would keep fewer PR slots than it holds needs GitHub to
# list an online ordinary runner of another machine (`<machine>-<n>`, with the workflow label):
# with none, the other machine is down, unreachable or not installed, and both counts stay. What
# it holds is what its last install recorded, or else its own runners with the workflow label; a
# first install holds none. A list it cannot read stops it too.
if [ -n "$other_machines" ]; then
  if ! listed=$(gh api --paginate "repos/$repository/actions/runners?per_page=100" --jq \
    '.runners[] | [.name, .status, ([.labels[].name] | join(","))] | @tsv'); then
    echo "Could not read GitHub's runners, so $machine cannot tell whether the other machines took its PR jobs: nothing was changed." >&2
    exit 1
  fi
  others_ordinary="^($(printf '%s\n' $other_machines | paste -sd '|'))-[1-9][0-9]*\$"
  held=$recorded_jobs
  if [ -z "$held" ]; then
    held=$(printf '%s\n' "$listed" | awk -F '\t' -v own="$own_runner" -v label="$label" '
      $1 ~ own && index("," $3 ",", "," label ",") {n++} END {print n+0}')
  fi
  if [ "$runners" -lt "$held" ]; then
    taken=$(printf '%s\n' "$listed" | awk -F '\t' -v others="$others_ordinary" -v label="$label" '
      $1 ~ others && $2 == "online" && index("," $3 ",", "," label ",") {n++} END {print n+0}')
    if [ "$taken" -lt 1 ]; then
      echo "$machine would give up $((held - runners)) PR slots ($held to $runners), but GitHub lists no online ordinary runner of $other_machines with $label. Install the receiving machine first and wait for its runners (README.md, \"Moving a machine's PR slots\"): nothing was changed." >&2
      exit 1
    fi
  fi
fi

mkdir -p "$installed" "$units"
prefix=$prefix sh "$here/install-github-budget.sh"
# The runner balancer leaves the runners alone until the install ends: it would find them stopped
# and start more, or read pool-settings.sh half written. It holds the same lock while it changes
# them, for at most the few minutes a runner takes to start.
mkdir -p "$HOME/.local/state/workflow"
exec 8>>"$HOME/.local/state/workflow/runner-balance.lock"
flock -w 300 8 || { echo "The runner balancer did not let go of its lock in 5 minutes." >&2; exit 1; }
# The runner balancer's own lines (its bounds, `balance off`): the machine settings', or, when
# they name none, the installed ones, which outlast a reinstall. The counts are the ones this
# install was given.
balance_lines=$(python3 "$here/balance_runners.py" kept "$machine_settings")
[ -n "$balance_lines" ] ||
  balance_lines=$(python3 "$here/balance_runners.py" kept "$installed/pool-settings.sh")
# What every script of this machine reads after settings.sh: the repository's names, its main
# checkout, which machine this is and its counts. Each value is quoted for sh. Written before the
# scripts are copied: a running service that restarts before this install stops it runs the new
# scripts, which need these names.
quoted() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
{
  printf 'repository=%s\n' "$(quoted "$repository")"
  printf 'prefix=%s\n' "$(quoted "$prefix")"
  printf 'label=%s\n' "$(quoted "$label")"
  printf 'planner_label=%s\n' "$(quoted "$planner_label")"
  printf 'checkout=%s\n' "$(quoted "$checkout")"
  # Installed image/cache commands must keep the external file too, without unit changes.
  if [ "$SHIPLINE_CONFIG" != "$SHIPLINE_REPO/shipline.json" ]; then
    printf 'installation_config=%s\n' "$(quoted "$SHIPLINE_CONFIG")"
  fi
  printf 'machine=%s\n' "$(quoted "$machine")"
  printf 'roles=%s\n' "$(quoted "$roles")"
  printf 'other_machines=%s\n' "$(quoted "$other_machines")"
  printf 'ordinary_jobs=%s\n' "$runners"
  printf 'installed_slots=%s\n' "$runners"
  printf 'ordinary_blacksmith_jobs=%s\n' "$ordinary_blacksmith_jobs"
  printf 'ordinary_spares=%s\n' "$spares"
  printf 'docker_runners=%s\n' "$docker_runners"
  [ -z "$balance_lines" ] || printf '%s\n' "$balance_lines"
  [ -z "$job_memory" ] || printf 'job_memory=%s\n' "$job_memory"
} >"$installed/pool-settings.tmp"
mv "$installed/pool-settings.tmp" "$installed/pool-settings.sh"
cp -L "$here"/*.sh "$here"/*.py "$installed/"
cp "$tools/identity-folder" "$installed/"
rm -rf "$installed/vm" "$installed/systemd"
cp -R "$here/vm" "$installed/vm"
cp -R "$here/systemd" "$installed/systemd"
rendered=$tools/units
cp "$rendered/$container-firewall.service" "$installed/systemd/"
cp "$rendered/$container-planner.service" "$rendered/$container"-main*.service "$units/"
cp "$rendered/$container@.service" "$rendered/$container-docker@.service" \
  "$rendered/$container-spare.service" "$rendered/$container-lend.service" \
  "$rendered/$container"-image.* "$rendered/$container"-usage.* \
  "$rendered/$container"-balance.* "$units/"
if [ -n "$hub_cache" ]; then
  cp "$rendered/$hub_cache_container.service" "$rendered/$hub_cache_container"-watch.* "$units/"
fi

RUNNER_PREFIX=$prefix RUNNER_CHECKOUT=$checkout "$installed/build-images.sh"

# Runners of an earlier install stop first: they hold containers of the old image, and their
# networks may sit on addresses the new ones need. Each runner makes its own network again when it
# starts.
systemctl --user daemon-reload
# Save the concrete names before stopping anything, including services still starting up. Read
# separately from awk so a failed systemctl call cannot silently leave recovery with no names.
running=$(systemctl --user list-units --type=service --state=active,activating,reloading \
  --no-legend --plain $container-main.service $container-main-2.service $container-planner.service \
  $container-spare.service $container-lend.service \
  "$container@*.service" "$container-docker@*.service")
stopped=$(printf '%s\n' "$running" | awk '{print $1}')
starting_pool=0
recover_runners() {
  result=$?
  trap - EXIT INT TERM
  rm -rf "$tools"
  if [ "$result" -ne 0 ]; then
    echo "Install failed; restarting the runner services that were running before it." >&2
    # A resize can put a new ordinary runner on an old Docker runner's address. Remove the
    # partial new pool before restoring old services, so two jobs never share that network.
    if [ "$starting_pool" -eq 1 ] && ! systemctl --user stop $container-spare.service \
      "$container@*.service" "$container-docker@*.service" $container-main.service $container-main-2.service; then
      echo "Could not stop the partial new pool; restoring old services could share job networks. To recover, run:" >&2
      printf '%s' "systemctl --user stop $container-spare.service '$container@*.service' '$container-docker@*.service' $container-main.service $container-main-2.service" >&2
      if [ -n "$stopped" ]; then
        printf '%s' ' && systemctl --user start' >&2
        printf ' %s' $stopped >&2
      fi
      printf '\n' >&2
      exit "$result"
    fi
    for service in $stopped; do
      # Try every service even if one cannot start. Keep the install's original exit status.
      systemctl --user start "$service" ||
        echo "Could not restart $service. Retry with: systemctl --user start $service" >&2
    done
  fi
  exit "$result"
}
trap recover_runners EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl --user stop $container-main.service $container-main-2.service $container-planner.service $container-spare.service $container-lend.service "$container@*.service" "$container-docker@*.service"
# The planner's network goes too: it starts again on one made from the current `subnet`.
for old_network in $(docker network ls --quiet --filter "name=^$network"); do
  docker network rm "$old_network" >/dev/null
done
# Restore CI routing before registration retries and cache startup. Image builds happened before
# stopping any runners, so an existing planner stayed available during them.
if has_role planner; then
  systemctl --user enable --now $container-planner.service
fi

delete_retired_runner() {
  retries=0
  while true; do
    if error=$(gh api -X DELETE "repos/$repository/actions/runners/$1" 2>&1); then
      return 0
    else
      result=$?
    fi
    # Only GitHub's busy-registration error is transient here. Authentication, validation and
    # other failures must still fail installation (and let the exit trap restore the runners).
    case "$error" in
      *"currently running a job"*"HTTP 422"*) ;;
      *) printf '%s\n' "$error" >&2; return "$result" ;;
    esac
    if [ "$retries" -ge 20 ]; then
      echo "Warning: retired runner registration $1 is still busy after 300 seconds; leaving it for GitHub to remove and continuing install." >&2
      return 0
    fi
    echo "Retired runner registration $1 is still busy; retrying in 15 seconds." >&2
    sleep 15
    retries=$((retries + 1))
  done
}
# Stopping an idle ephemeral container does not deregister it. On a smaller install its retired
# name will never be replaced, and the supervisor would reserve its old workflow label forever
# (or until GitHub removes the offline registration). Ordinary runners are stopped now, so names
# outside the new ordinary range are definitively retired. Do not touch Docker or foreign names,
# nor the runners of another machine (names.sh), whose numbers are counted by that machine.
# Read separately from the filtering pipeline so a failed/partial API read aborts installation.
registered=$(gh api --paginate "repos/$repository/actions/runners" --jq '.runners[] | [.id, .name] | @tsv')
retired=$(printf '%s\n' "$registered" | awk -F '\t' -v total="$ordinary_total" \
  -v own="$own_runner" -v at="$own_number_at" \
  '$1 ~ /^[0-9]+$/ && $2 ~ own && substr($2, at)+0 > total {print $1}')
for id in $retired; do
  delete_retired_runner "$id"
done
# Runners of an earlier install that this one no longer asks for must not start with the machine:
# a Docker-capable runner left enabled could take the place in the address range of an ordinary
# one, and the two jobs would share a network. Every runner is switched off here; the ones asked
# for are switched on below.
rm -f "$units"/default.target.wants/$container@*.service \
  "$units"/default.target.wants/$container-docker@*.service
has_role planner || rm -f "$units"/default.target.wants/$container-planner.service
has_role main || rm -f "$units"/default.target.wants/$container-main.service \
  "$units"/default.target.wants/$container-main-2.service \
  "$units"/default.target.wants/$container-lend.service
has_role ordinary || rm -f "$units"/default.target.wants/$container-spare.service
has_role ordinary || has_role docker || rm -f "$units"/timers.target.wants/$container-balance.timer
systemctl --user daemon-reload

# The mirror is an optimization, never a runner dependency. Its service retries if Docker Hub
# is temporarily unavailable even for the registry image; Docker in a VM falls back to Hub.
if [ -n "$hub_cache" ]; then
  systemctl --user enable --now "$hub_cache_container.service" ||
    echo "The Docker Hub cache could not start; jobs will pull directly from Docker Hub." >&2
  systemctl --user enable --now "$hub_cache_container-watch.timer"
elif [ -e "$units/$hub_cache_container.service" ]; then
  # A cache the repository no longer runs, or a machine no longer docker: its units go.
  systemctl --user disable --now "$hub_cache_container.service" "$hub_cache_container-watch.timer" ||
    true
  rm -f "$units/$hub_cache_container.service" "$units/$hub_cache_container"-watch.*
  systemctl --user daemon-reload
fi
systemctl --user enable --now $container-image.timer
# One line a minute of what the machine has free (usage.py), on every machine.
systemctl --user enable --now $container-usage.timer
starting_pool=1
# Main's runner takes place 14, where an older install's Docker runner may be: it is part of the
# new pool that a failed install stops before restoring the old services.
if has_role main; then
  systemctl --user enable --now $container-main.service
  systemctl --user enable --now $container-main-2.service
fi
number=1
while [ "$number" -le "$ordinary_total" ]; do
  systemctl --user enable --now "$container@$number.service"
  number=$((number + 1))
done
if has_role ordinary; then
  systemctl --user enable --now $container-spare.service
fi
# After main's runners and the PR pool: it lends one of main's idle runners to PR jobs.
if has_role main; then
  systemctl --user enable --now $container-lend.service
fi
number=1
while [ "$number" -le "$docker_runners" ]; do
  systemctl --user enable --now "$container-docker@$number.service"
  number=$((number + 1))
done
# After the runners it counts: it moves them between the kinds within the bounds of
# pool-settings.sh, and changes nothing without them (balance_runners.py).
if has_role ordinary || has_role docker; then
  systemctl --user enable --now "$container-balance.timer"
fi

main_runners=0
! has_role main || main_runners=2
echo "Installed on $machine: $runners PR job slots, $spares spare, $main_runners reserved for main, and $docker_runners with Docker."
! has_role planner || echo "This machine also runs the CI planner."
identity=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import github_identity; print(github_identity.folder())' "$installed")
if [ -e "$identity/token" ] || [ -e "$identity/app-id" ]; then
  echo "GitHub: the runner programs use their own GitHub identity, from $identity."
else
  echo "GitHub: the runner programs use your gh login and its hourly budget. To give them their own"
  echo "  identity, run this again with --github-token or --github-app."
fi
echo "VM idle memory: virtio-balloon-pci,free-page-reporting=on (the job memory cap is unchanged)."
echo "They wait until the firewall is in place:"
echo "  sudo sh $installed/install-firewall.sh"
if [ "$docker_runners" -gt 0 ]; then
  echo "The runner with Docker then checks by itself that a job in it is shut in (about two"
  echo "minutes) and takes no job before that passed. To see the check, run it yourself:"
  echo "  sh $installed/check-docker-runner.sh"
fi

# Services of a user stop when that user logs out and do not start with the machine, unless the
# user is set to linger.
if [ "$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)" != yes ]; then
  echo "The runners will stop when you log out. To keep them up, also after a restart:"
  echo "  sudo loginctl enable-linger $(id -un)"
fi
