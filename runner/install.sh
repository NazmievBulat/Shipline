#!/bin/sh
# Run once on each machine that runs runners, as yourself (not root):
#
#   sh install.sh [--machine <name>] [--github-token <file> | --github-app <app id> <key file>]
#                 [how many PR job slots] [how many with Docker]
#
# The name is one of machines/: the first install on a machine must give it, a later one may leave
# it out and takes the recorded one. machines/<name>.sh says what the machine runs (its roles) and
# how many runners of each kind it gets when no count is given. On a machine with the role `main`
# two main runners are added, and with the role `ordinary` one PR spare.
#
# --github-token and --github-app give the runner programs a GitHub identity of their own
# (github_identity.py, README.md "The runners' own GitHub identity"): the file is checked against
# GitHub and copied to ~/.config/example-app-runner, for this user alone, and never printed. Once
# given, a later install keeps it. Without one they use your gh login.
#
# It copies these files out of the repository, builds the images and starts the runners as services
# of your user. The copy matters: what runs on this machine with your GitHub login must not change
# because a pull request changed a file. To take a newer version, run this again from main.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
installed=$HOME/.local/share/example-app-runner
units=$HOME/.config/systemd/user
. "$here/settings.sh"

asked=
github_token=
github_app_id=
github_app_key=
while [ $# -gt 0 ]; do
  case "$1" in
    --machine)
      [ $# -ge 2 ] || { echo "--machine needs a name." >&2; exit 1; }
      asked=$2
      shift 2
      ;;
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
known=$(cd "$here/machines" && ls -- *.sh | sed 's/\.sh$//' | tr '\n' ' ')
# A machine installed before machines had names is the home machine (settings.sh).
recorded=
if [ -f "$installed/pool-settings.sh" ]; then
  recorded=$(
    machine=home
    . "$installed/pool-settings.sh"
    printf '%s' "$machine"
  )
fi
# Two machines with the same name would take each other's runner registrations (`--replace`) and
# retire each other's runners, so a machine is never taken to be one it was not named.
if [ -z "$asked$recorded" ]; then
  echo "Say which machine this is: sh install.sh --machine <name>, one of: $known" >&2
  exit 1
fi
if [ -n "$asked" ] && [ -n "$recorded" ] && [ "$asked" != "$recorded" ]; then
  echo "This machine's runners were installed as $recorded, not $asked. To rename it, stop its runners and remove $installed first." >&2
  exit 1
fi
machine=${asked:-$recorded}
# A name is one word of small letters and digits: it starts the runners' names (names.sh).
case "$machine" in
  '' | *[!a-z0-9]*) known_machine=no ;;
  *) case " $known" in *" $machine "*) known_machine=yes ;; *) known_machine=no ;; esac ;;
esac
if [ "$known_machine" = no ]; then
  echo "There is no machine '$machine'; the machines are: $known" >&2
  exit 1
fi
other_machines=$(printf '%s\n' $known | grep -vx -- "$machine" | tr '\n' ' ' | sed 's/ $//')
# The machine's own roles and counts, in place of the defaults of settings.sh.
. "$here/machines/$machine.sh"
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
  { has_role ordinary && [ "$runners" -lt 1 ]; } ||
  [ $((runners + spares + docker_runners)) -gt "$most_runners" ]; then
  echo "There must be at least 1 ordinary runner, and at most $most_runners runners of both kinds together (including the spare)." >&2
  exit 1
fi
ordinary_total=$((runners + spares))
# A machine that gives up no PR slot while Blacksmith has budget keeps all of them.
[ -n "${ordinary_blacksmith_jobs:-}" ] || ordinary_blacksmith_jobs=$runners
[ "$ordinary_blacksmith_jobs" -le "$runners" ] || ordinary_blacksmith_jobs=$runners
# The job of a Docker-capable runner is inside a virtual machine, which needs this device.
# RUNNER_KVM_DEVICE names another path for it, for the tests of this script.
kvm=${RUNNER_KVM_DEVICE:-/dev/kvm}
if [ "$docker_runners" -gt 0 ] && ! [ -e "$kvm" ]; then
  echo "This machine cannot run virtual machines (no /dev/kvm), so it gets no runner with Docker." >&2
  docker_runners=0
fi

# Before anything is stopped or copied: a refused identity leaves the machine as it was. The
# services running now take the new one at their next call.
if [ -n "$github_token" ]; then
  python3 "$here/github_identity.py" install --repository "$repository" --token "$github_token"
elif [ -n "$github_app_id" ]; then
  python3 "$here/github_identity.py" install --repository "$repository" \
    --app "$github_app_id" "$github_app_key"
fi
# This script's own calls are the runner programs' too.
gh() {
  python3 "$here/github_budget.py" wait --identity runner runner-install &&
    python3 "$here/github_budget.py" call --identity runner runner-install "$@"
}

mkdir -p "$installed" "$units"
sh "$here/install-github-budget.sh"
cp "$here/Dockerfile" "$here/vm.Dockerfile" "$here"/*.sh "$here"/*.py "$installed/"
rm -rf "$installed/vm"
cp -R "$here/vm" "$installed/vm"
mkdir -p "$installed/systemd"
cp "$here"/systemd/example-app-runner-firewall.service "$installed/systemd/"
cp "$here"/systemd/example-app-runner-planner.service \
  "$here"/systemd/example-app-runner-main*.service "$units/"
cp "$here"/systemd/example-app-runner@.service "$here"/systemd/example-app-runner-docker@.service \
  "$here"/systemd/example-app-hub-cache.service "$here"/systemd/example-app-hub-cache-watch.* \
  "$here"/systemd/example-app-runner-spare.service "$here"/systemd/example-app-runner-lend.service \
  "$here"/systemd/example-app-runner-image.* "$here"/systemd/example-app-runner-usage.* \
  "$here"/systemd/example-app-runner-balance.* "$units/"

"$installed/build-images.sh"

# Runners of an earlier install stop first: they hold containers of the old image, and their
# networks may sit on addresses the new ones need. Each runner makes its own network again when it
# starts.
systemctl --user daemon-reload
# Save the concrete names before stopping anything, including services still starting up. Read
# separately from awk so a failed systemctl call cannot silently leave recovery with no names.
running=$(systemctl --user list-units --type=service --state=active,activating,reloading \
  --no-legend --plain example-app-runner-main.service example-app-runner-main-2.service example-app-runner-planner.service \
  example-app-runner-spare.service example-app-runner-lend.service \
  'example-app-runner@*.service' 'example-app-runner-docker@*.service')
stopped=$(printf '%s\n' "$running" | awk '{print $1}')
starting_pool=0
recover_runners() {
  result=$?
  trap - EXIT INT TERM
  if [ "$result" -ne 0 ]; then
    echo "Install failed; restarting the runner services that were running before it." >&2
    # A resize can put a new ordinary runner on an old Docker runner's address. Remove the
    # partial new pool before restoring old services, so two jobs never share that network.
    if [ "$starting_pool" -eq 1 ] && ! systemctl --user stop example-app-runner-spare.service \
      'example-app-runner@*.service' 'example-app-runner-docker@*.service' example-app-runner-main.service example-app-runner-main-2.service; then
      echo "Could not stop the partial new pool; restoring old services could share job networks. To recover, run:" >&2
      printf '%s' "systemctl --user stop example-app-runner-spare.service 'example-app-runner@*.service' 'example-app-runner-docker@*.service' example-app-runner-main.service example-app-runner-main-2.service" >&2
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
# The runner balancer's own lines (its bounds, `balance off`) outlast a reinstall; the counts are
# the ones this install was given.
balance_lines=$(python3 "$here/balance_runners.py" kept "$installed/pool-settings.sh")
systemctl --user stop example-app-runner-main.service example-app-runner-main-2.service example-app-runner-planner.service example-app-runner-spare.service example-app-runner-lend.service 'example-app-runner@*.service' 'example-app-runner-docker@*.service'
# What every script of this machine reads after settings.sh: which machine it is and its counts.
{
  printf 'machine=%s\n' "$machine"
  printf "roles='%s'\n" "$roles"
  printf "other_machines='%s'\n" "$other_machines"
  printf 'ordinary_jobs=%s\n' "$runners"
  printf 'ordinary_blacksmith_jobs=%s\n' "$ordinary_blacksmith_jobs"
  printf 'docker_runners=%s\n' "$docker_runners"
  [ -z "$balance_lines" ] || printf '%s\n' "$balance_lines"
} >"$installed/pool-settings.sh"
# The planner's network goes too: it starts again on one made from the current `subnet`.
for old_network in $(docker network ls --quiet --filter "name=^$network"); do
  docker network rm "$old_network" >/dev/null
done
# Restore CI routing before registration retries and cache startup. Image builds happened before
# stopping any runners, so an existing planner stayed available during them.
if has_role planner; then
  systemctl --user enable --now example-app-runner-planner.service
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
. "$here/names.sh"
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
rm -f "$units"/default.target.wants/example-app-runner@*.service \
  "$units"/default.target.wants/example-app-runner-docker@*.service
has_role planner || rm -f "$units"/default.target.wants/example-app-runner-planner.service
has_role main || rm -f "$units"/default.target.wants/example-app-runner-main.service \
  "$units"/default.target.wants/example-app-runner-main-2.service \
  "$units"/default.target.wants/example-app-runner-lend.service
has_role ordinary || rm -f "$units"/default.target.wants/example-app-runner-spare.service
has_role ordinary || has_role docker ||
  rm -f "$units"/timers.target.wants/example-app-runner-balance.timer
systemctl --user daemon-reload

# The mirror is an optimization, never a runner dependency. Its service retries if Docker Hub
# is temporarily unavailable even for the registry image; Docker in a VM falls back to Hub.
if has_role docker; then
  systemctl --user enable --now example-app-hub-cache.service ||
    echo "The Docker Hub cache could not start; jobs will pull directly from Docker Hub." >&2
  systemctl --user enable --now example-app-hub-cache-watch.timer
fi
systemctl --user enable --now example-app-runner-image.timer
# One line a minute of what the machine has free (usage.py), on every machine.
systemctl --user enable --now example-app-runner-usage.timer
starting_pool=1
# Main's runner takes place 14, where an older install's Docker runner may be: it is part of the
# new pool that a failed install stops before restoring the old services.
if has_role main; then
  systemctl --user enable --now example-app-runner-main.service
  systemctl --user enable --now example-app-runner-main-2.service
fi
number=1
while [ "$number" -le "$ordinary_total" ]; do
  systemctl --user enable --now "example-app-runner@$number.service"
  number=$((number + 1))
done
if has_role ordinary; then
  systemctl --user enable --now example-app-runner-spare.service
fi
# After main's runners and the PR pool: it lends one of main's idle runners to PR jobs.
if has_role main; then
  systemctl --user enable --now example-app-runner-lend.service
fi
number=1
while [ "$number" -le "$docker_runners" ]; do
  systemctl --user enable --now "example-app-runner-docker@$number.service"
  number=$((number + 1))
done
# After the runners it counts: it moves them between the kinds within the bounds of
# pool-settings.sh, and changes nothing without them (balance_runners.py).
if has_role ordinary || has_role docker; then
  systemctl --user enable --now example-app-runner-balance.timer
fi

main_runners=0
! has_role main || main_runners=2
echo "Installed on $machine: $runners PR job slots, $spares spare, $main_runners reserved for main, and $docker_runners with Docker."
! has_role planner || echo "This machine also runs the CI planner."
identity=$HOME/.config/example-app-runner
if [ -e "$identity/token" ] || [ -e "$identity/app-id" ]; then
  echo "GitHub: the runner programs use their own GitHub identity, from $identity."
else
  echo "GitHub: the runner programs use your gh login and its hourly budget. To give them their own"
  echo "  identity, see \"The runners' own GitHub identity\" in .github/runner/README.md."
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
