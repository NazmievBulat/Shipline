# Read after settings.sh and pool-settings.sh. Every name of the pool comes from two words of the
# repository's shipline.json, `prefix` (`runner.prefix`) and `label` (`runner.label`), and the
# machine's own name: install.sh writes all three into pool-settings.sh.
: "${prefix:?names.sh needs the prefix (runner.prefix), which install.sh writes into pool-settings.sh}"
label=${label:-$prefix-home}

# Where the installed scripts are, and the images, networks and containers the runners use. The
# units are named alike (systemd/, render-units.sh).
installed=$HOME/.local/share/$prefix-runner
image=$prefix-runner
docker_image=$prefix-runner-docker
network=$prefix-runner
container=$prefix-runner
hub_cache_network=$prefix-hub-cache
hub_cache_container=$prefix-hub-cache

# The name the workflows ask for in `runs-on`. Every machine's runners carry the same labels, so
# GitHub gives a job to whichever machine has a runner free: every ordinary container registers
# with the standby label until it takes a slot, the merge train's first member's jobs ask for the
# first label, main's for the main label, the merge gate's first run of a prefix for main's batch
# label (which the lender gives main's runners while no push to main waits), Docker's and the
# planner's for their own.
standby_label=$label-standby
first_label=$label-first
main_label=$label-main
main_batch_label=$label-main-batch
docker_label=$label-docker
planner_label=${planner_label:-$label-planner}

# Each machine names its runners after itself (~/.config/shipline/runner.sh): its PR runners are
# <name>-1, <name>-2 and on, its Docker runners <name>-docker-1 and on. A script here changes only
# registrations of its own machine: those of another machine are that machine's to retire, label
# or count.
own_name=${machine:-}
own_runner="^$own_name-[0-9]+\$"
own_number_at=$((${#own_name} + 2))
# Names of the other machines' runners. With no other machine it matches no name.
others='^$'
if [ -n "${other_machines:-}" ]; then
  others="^($(printf '%s\n' $other_machines | paste -sd '|'))-"
fi
