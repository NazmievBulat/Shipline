# Shared by the scripts in this folder. Change a value here, then run install.sh again.
#
# What names the repository comes from its shipline.json (`repository`, `runner.prefix`,
# `runner.label`, `runner.planner_label`), and what is this machine's own from
# ~/.config/shipline/runner.sh (`machine`, `roles`, `other_machines` and the counts). install.sh
# writes both into pool-settings.sh beside the installed scripts, which the scripts read after
# this file; names.sh then makes every name from them.

# Three ordinary runners register; only two carry the PR workflow label at once. The spare is
# already online when an ephemeral runner disappears, and needs only a label to take its place.
# These counts hold for a script run without an install; install.sh takes each machine's own.
ordinary_jobs=2
# Two slots are reserved for main, even when hosted minutes run out.
main_place=14
# Relative CPU weight under contention; ordinary and Docker runners keep Docker's 1024.
main_cpu_shares=8192
# The merge train's first member's label is on every PR slot beside the workflow label
# (keep-spare.sh), and on one of main's runners while it is lent to PR jobs (lend_runners.py). At
# most main_lent are lent, with one online main runner always kept unlent; the lender looks at the
# queue every lend_poll_seconds.
main_lent=1
lend_poll_seconds=20
# After the Blacksmith trial and reinstall, use one PR slot while CI_BLACKSMITH and
# CI_OVERFLOW are both on and a full run still fits below 90% (actual minutes plus
# reservations). Keep three PR containers registered so the second PR slot can return
# without restarting services. Main has its own two active slots.
ordinary_blacksmith_jobs=1
ordinary_spares=1
spare_poll_seconds=5
# The addresses the firewall treats as CI containers. Each runner gets a network of its own inside
# this range, sixteen addresses each, named after `network` (names.sh) and its place in the range
# (runner-container.sh). One shared network would not do: containers on the same network reach
# each other without passing the firewall. The ordinary runners take the places from 1 upwards,
# the Docker-capable ones from the last downwards, and place 0 is check-docker-runner.sh's.
subnet=172.30.0.0/24
# Places 13 and 14 belong to main; place 15 belongs to the planner.
most_runners=12
# The most memory one job may use.
memory=10g
# An address nobody owns (it is reserved for examples). The firewall answers it with "refused",
# which is how keep-runner.sh can tell, without being root, that the firewall is in place.
firewall_marker=192.0.2.1

# The Docker-capable runner, which a workflow asks for by a label of its own (names.sh), so it
# never takes a job that does not need Docker and no other job waits for it. Its virtual machine,
# one for each job, gets this much; the memory is taken as the job uses it, not at once.
vm_memory=4
vm_processors=8
# The most one job may write to its disk: the images it builds, its containers and its checkout.
vm_disk=40G

# The Docker Hub cache, when the repository runs one (`runner.hub_cache`): a network of its own
# (names.sh), outside the runner range, and no port published on this machine. Keep the address
# and port in step with vm/guest/usr/local/bin/hub-mirror.
hub_cache_subnet=172.31.255.0/29
hub_cache_address=172.31.255.2
hub_cache_port=5000
# The most the cache may keep on this machine's disk. A job can pull any image of Docker Hub
# through it, so above this the cache starts again empty (looked at every 10 minutes).
hub_cache_most_megabytes=20480

# The runner balancer (balance_runners.py) reads its own lines of pool-settings.sh, among them
# `balance off`, which stops it. Every other script reads that line as nothing.
balance() { :; }

# One planning slot, outside the ordinary/Docker queue; place 15 stays in the firewall range.
planner_place=15
planner_memory=1g
planner_cpus=1
