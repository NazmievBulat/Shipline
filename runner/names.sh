# Read after settings.sh and pool-settings.sh. Each machine names its runners after itself
# (machines/<name>.sh): its PR runners are <name>-1, <name>-2 and on, its Docker runners
# <name>-docker-1 and on. A script here changes only registrations of its own machine: those of
# another machine are that machine's to retire, label or count.
own_runner="^$machine-[0-9]+\$"
own_number_at=$((${#machine} + 2))
# Names of the other machines' runners. With no other machine it matches no name.
others='^$'
if [ -n "${other_machines:-}" ]; then
  others="^($(printf '%s\n' $other_machines | paste -sd '|'))-"
fi
