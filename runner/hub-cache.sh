#!/bin/sh
# Runs the Docker Hub cache of the Docker-capable runners' virtual machines with the repository's
# own command (`runner.hub_cache`), on main's files (main-command.sh), by <prefix>-hub-cache.service;
# `hub-cache.sh watch`, every 10 minutes by <prefix>-hub-cache-watch.timer, empties the cache
# when it holds more than its limit. The command gets where the cache must be: the container
# HUB_CACHE_CONTAINER on the network HUB_CACHE_NETWORK (HUB_CACHE_SUBNET), at HUB_CACHE_ADDRESS
# and HUB_CACHE_PORT, which the virtual machines and the firewall expect (settings.sh), and its
# limit, HUB_CACHE_MOST_MEGABYTES. Without the command no cache is installed, and the virtual
# machines pull from Docker Hub.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/settings.sh"
if [ -f "$here/pool-settings.sh" ]; then
  . "$here/pool-settings.sh"
fi
prefix=${RUNNER_PREFIX:-${prefix:-}}
. "$here/names.sh"
. "$here/main-command.sh"
HUB_CACHE_CONTAINER=$hub_cache_container HUB_CACHE_NETWORK=$hub_cache_network \
  HUB_CACHE_SUBNET=$hub_cache_subnet HUB_CACHE_ADDRESS=$hub_cache_address \
  HUB_CACHE_PORT=$hub_cache_port HUB_CACHE_MOST_MEGABYTES=$hub_cache_most_megabytes \
  on_main hub_cache "$@"
