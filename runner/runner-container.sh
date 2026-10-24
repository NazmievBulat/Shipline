# Read by keep-runner.sh and check-docker-runner.sh, after settings.sh. How a runner's network is
# made and how its container is started are written once, here, so that the check starts exactly
# the container a job gets.

# The network at one place of the range: place 1 is 172.30.0.16 to .31, place 2 the sixteen after,
# and so on. Docker lets nothing pass from one network to another, so a job cannot reach the job
# of another runner.
network_at() {
  docker network inspect "$network-$1" >/dev/null 2>&1 ||
    docker network create --subnet "${subnet%.*}.$(($1 * 16))/28" "$network-$1" >/dev/null
}

# Place 0 is check-docker-runner.sh's, in two halves of eight addresses: the network of the check,
# and the network of a container that stands for the job of another runner. It is inside the range
# of the runners' networks, as the job of a second runner with Docker is, so the check meets the
# same rules a job of one runner meets on its way to the job of another.
check_network_at() {
  docker network create --subnet "${subnet%.*}.$(($1 * 8))/29" "$2" >/dev/null
}

# With the firewall in place a connection to the marker address is refused at once; without it the
# connection goes nowhere. Only "refused" counts: a router that answers "no way there" must not
# pass for the firewall.
firewall_is_in_place() {
  docker run --rm --network "$1" --entrypoint node "$image" --eval '
    const socket = require("node:net").connect(9, process.argv[1])
    socket.setTimeout(5000, () => process.exit(1))
    socket.on("connect", () => process.exit(1))
    socket.on("error", (error) => process.exit(error.code === "ECONNREFUSED" ? 0 : 1))
  ' "$firewall_marker"
}

# Starts one container and waits for it to end: start_container <kind> <name> <network> [more
# options of `docker run`]. Standard input goes to the container.
#
# The container gets no folder of this machine and no access to Docker. It cannot become root, and
# its memory and process count are capped so a job that runs away cannot take the machine down.
#
# tcp_retries2: how often the kernel sends data again on a connection that is not answered before
# it calls the connection dead. With Linux's own 15 that takes a quarter of an hour, and a
# program that waits for the answer without a limit of its own (`bun install`) hangs that long;
# with 7 it takes under a minute, and the program tries again on a new connection (README.md,
# "Downloads that hang"). It is a setting of the container's own network and gives it no right.
# The connections of the virtual machine are made by QEMU in this container, so it holds for them.
start_container() {
  container_kind=$1
  set -- "$@" --rm -i \
    --name "$2" \
    --network "$3" \
    --security-opt no-new-privileges \
    --cap-drop ALL \
    --pids-limit 8192 \
    --sysctl net.ipv4.tcp_retries2=7
  shift 3
  if [ "$container_kind" = main ]; then
    set -- "$@" --cpu-shares "$main_cpu_shares"
  fi
  if [ "$container_kind" = docker ]; then
    # The one thing more this container gets: the device that runs virtual machines, and the group
    # that may open it. The job is inside the virtual machine (vm.Dockerfile); the memory cap
    # leaves a gigabyte for the program that runs the machine.
    docker run "$@" \
      --device /dev/kvm \
      --group-add "$(stat -c %g /dev/kvm)" \
      --memory "$((vm_memory + 1))g" \
      --env VM_MEMORY="${vm_memory}G" \
      --env VM_PROCESSORS="$vm_processors" \
      --env VM_DISK="$vm_disk" \
      "$docker_image"
  elif [ "$container_kind" = planner ]; then
    docker run "$@" --memory "$planner_memory" --cpus "$planner_cpus" \
      --volume "$HOME/.local/state/ci-overflow/home:/home/runner/.local/state/ci-overflow/home:ro" \
      --volume "$HOME/.local/state/ci-overflow/ledger:/home/runner/.local/state/ci-overflow/ledger" \
      "$image"
  else
    # An ordinary job gets this machine's own cap when it names one (settings.sh); main's keep the
    # shared one.
    cap=$memory
    [ "$container_kind" != ordinary ] || cap=${job_memory:-$memory}
    docker run "$@" --memory "$cap" --shm-size 4g "$image"
  fi
}
