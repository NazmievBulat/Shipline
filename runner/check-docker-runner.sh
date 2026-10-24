#!/bin/sh
# Run on the home machine, as yourself, after install.sh and install-firewall.sh:
#
#   sh check-docker-runner.sh
#
# It shows that a job on the Docker-capable runner is shut in. It starts the same container a job
# gets, twice, with a check in place of the job (vm/guest/usr/local/bin/check-isolation), and
# looks at the container from outside. A job must not reach this machine, the home network or
# another container; it must not have this machine's Docker or any of its folders; and the second
# start must find nothing the first one made. The one place it may reach is the registry port of
# the Docker Hub cache, when that runs; another port of the cache must stay out of reach. It ends
# with "The Docker-capable runner is shut in." and status 0 only when all of that held. It takes
# about two minutes and no job is disturbed.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/settings.sh"
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
. "$here/names.sh"
. "$here/runner-container.sh"

name=$container-docker-check
neighbour=$network-check-neighbour
cache=$hub_cache_container
cache_other_port=$((hub_cache_port + 1))
cache_listens=
work=$(mktemp -d)
listener=

failed=0
good() { echo "ok      $*"; }
bad() {
  echo "FAILED  $*"
  failed=1
}

tidy() {
  [ -z "$listener" ] || kill "$listener" 2>/dev/null || true
  [ -z "$cache_listens" ] || docker exec "$cache" pkill -f "nc -lk -p $cache_other_port" 2>/dev/null || true
  docker rm -f "$name" "$neighbour" >/dev/null 2>&1 || true
  docker network rm "$neighbour" "$network-0" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap tidy EXIT
trap 'exit 130' INT TERM

# Place 0 of the range is kept for this check, so it shares a network with no runner. What an
# earlier check that was cut off left there goes first.
# RUNNER_CHECK_NETWORK names a network that is already there instead, for a machine whose range is
# taken up by one network of an older install; the neighbour then gets a network outside the range.
docker rm -f "$name" "$neighbour" >/dev/null 2>&1 || true
docker network rm "$neighbour" "$network-0" >/dev/null 2>&1 || true
check_network=${RUNNER_CHECK_NETWORK:-}
if [ -z "$check_network" ]; then
  check_network_at 0 "$network-0" || { echo "Could not make the network $network-0." >&2; exit 1; }
  check_network=$network-0
  check_network_at 1 "$neighbour" || { echo "Could not make the network $neighbour." >&2; exit 1; }
else
  docker network create "$neighbour" >/dev/null
fi
if ! firewall_is_in_place "$check_network"; then
  echo "The firewall for CI containers is not in place (see README.md). Nothing was checked." >&2
  exit 1
fi

# Something that listens on every address of this machine, and a container on another Docker
# network of the runners' range, as the job of another runner would be. Both are asked first: a
# silence the virtual machine meets later is then the firewall's doing, and not a listener that
# never started.
port=$((20000 + $$ % 20000))
python3 -m http.server --bind 0.0.0.0 --directory "$work" "$port" >/dev/null 2>&1 &
listener=$!
docker run --detach --rm --name "$neighbour" --network "$neighbour" --entrypoint node "$image" \
  --eval 'require("node:net").createServer((socket) => socket.end()).listen(8080)' >/dev/null
neighbour_address=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$neighbour")
# The Docker Hub cache, when it runs, gets something listening on a second port for the time of the
# check. The cache is an optimization and the check never waits for it: without it, the virtual
# machine only notes that it pulls from Docker Hub.
if [ "$(docker inspect --format '{{.State.Running}}' "$cache" 2>/dev/null)" = true ] &&
  docker exec --detach "$cache" nc -lk -p "$cache_other_port" -e true; then
  cache_listens=1
fi

answers() {
  python3 -c 'import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 3)' "$1" "$2" 2>/dev/null
}
# The neighbour is asked from inside itself, on its own address: the firewall lets nothing of the
# runners' range reach this machine, its answers to this machine included.
neighbour_answers() {
  docker exec "$neighbour" node --eval '
    const socket = require("node:net").connect(8080, process.argv[1])
    socket.setTimeout(3000, () => process.exit(1))
    socket.on("connect", () => process.exit(0))
    socket.on("error", () => process.exit(1))
  ' "$neighbour_address" 2>/dev/null
}
tries=0
# The cache's own BusyBox asks its second port, from inside the cache, as the neighbour is asked.
cache_answers() {
  [ -z "$cache_listens" ] || docker exec "$cache" nc -z -w 3 "$hub_cache_address" "$cache_other_port"
}
until answers 127.0.0.1 "$port" && neighbour_answers && cache_answers; do
  tries=$((tries + 1))
  [ "$tries" -lt 20 ] || { echo "The listeners of this check did not start. Nothing was checked." >&2; exit 1; }
  sleep 0.5
done

# The router stands for the home network. It must answer this machine on some port, or its silence
# towards the virtual machine would prove nothing.
router=$(ip -4 route show default | awk '{print $3; exit}')
router_port=
for candidate in 80 443 53; do
  if [ -n "$router" ] && answers "$router" "$candidate"; then
    router_port=$candidate
    break
  fi
done
if [ -z "$router_port" ]; then
  bad "the router of the home network (${router:-none found}) answers this machine on no port tried, so the home network cannot be checked"
fi

# A line that stands for the registration token: the check reads it as the runner reads the token,
# and must not find it anywhere afterwards.
secret=check-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')

# What the virtual machine is told: the cache it may reach, if it runs; then what it must not reach,
# every address of this machine, the neighbour, the cache's second port and the router of the home
# network.
{
  echo "$firewall_marker"
  docker info --format '{{.ID}}'
  echo "$secret"
  if [ -n "$cache_listens" ]; then echo "cache $hub_cache_address $hub_cache_port"; else echo cache; fi
  for address in $(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1); do
    echo "$address $port"
  done
  echo "$neighbour_address 8080"
  [ -z "$cache_listens" ] || echo "$hub_cache_address $cache_other_port"
  [ -z "$router_port" ] || echo "$router $router_port"
} >"$work/input"

for round in first second; do
  echo "The $round start:"
  start_container docker "$name" "$check_network" \
    --env VM_MODE=check --env RUNNER_REPOSITORY= --env RUNNER_NAME= --env RUNNER_LABEL= \
    <"$work/input" >"$work/output" 2>&1 &

  # The container, seen from outside while it runs: what it was given of this machine.
  tries=0
  until given=$(docker inspect --format \
    '{{len .Mounts}} {{.HostConfig.Privileged}} {{.HostConfig.CapAdd}} {{.HostConfig.CapDrop}} {{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}{{.HostConfig.SecurityOpt}} {{.Config.User}} {{.HostConfig.PidMode}}/{{.HostConfig.IpcMode}}/{{.HostConfig.UTSMode}}/{{.HostConfig.UsernsMode}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' \
    "$name" 2>/dev/null); do
    tries=$((tries + 1))
    [ "$tries" -lt 40 ] || break
    sleep 0.5
  done
  # The four names between the slashes are what the container shares with this machine: its
  # processes, its shared memory, its name, its users. `private` or nothing means not shared.
  if [ "${given:-}" = "0 false [] [ALL] /dev/kvm [no-new-privileges] runner /private// $(((vm_memory + 1) * 1073741824)) 8192" ]; then
    good "the container has no folder of this machine, no extra right, no device but /dev/kvm, and its memory and process count are capped"
  else
    bad "the container was given something it should not have: ${given:-it did not start}"
  fi

  wait $! || true
  # Only the lines of the check, without the clock the virtual machine's console puts before them
  # and the carriage return it puts after.
  tr -d '\r' <"$work/output" | sed -n 's/^.*vm-job\[[0-9]*\]: //p' >"$work/lines"
  cat "$work/lines"
  if [ "$(tail -n 1 "$work/lines")" != "The check passed." ]; then
    failed=1
    echo "What the container printed:"
    cat "$work/output"
  fi
  if [ -z "$(docker ps --all --quiet --filter "name=^$name$")" ]; then
    good "the container is gone, and the virtual machine's disk with it"
  else
    bad "the container is still there"
  fi
done

if [ "$failed" -eq 0 ]; then
  echo "The Docker-capable runner is shut in."
else
  echo "The Docker-capable runner is NOT shut in. Stop it until this is understood:"
  echo "  systemctl --user stop '$container-docker@*'"
  exit 1
fi
