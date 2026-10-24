#!/bin/sh
# Runs as root. CI containers may talk to the internet (GitHub, package registries) and to nothing
# else: not to this machine, not to the home network, not to each other. Safe to run again.
set -eu
. "$(dirname "$0")/settings.sh"

rule() {
  chain=$1
  shift
  iptables -w -C "$chain" "$@" 2>/dev/null || iptables -w -I "$chain" "$@"
}

# This machine itself.
rule INPUT -s "$subnet" -j DROP
# The home network, containers on other Docker networks (each runner has a network of its own),
# a private VPN range and link-local addresses.
for private in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  rule DOCKER-USER -s "$subnet" -d "$private" -j DROP
done
# The marker keep-runner.sh looks for.
rule DOCKER-USER -s "$subnet" -d "$firewall_marker" -p tcp -j REJECT --reject-with tcp-reset
# Insert last (at the top), ahead of the private-network drops. Only the registry port on
# this one cache container is reachable; the host and all other private endpoints stay blocked.
rule DOCKER-USER -s "$subnet" -d "$hub_cache_address" -p tcp --dport "$hub_cache_port" -j ACCEPT
