#!/usr/bin/env bash
set -euo pipefail

# Run with sudo if your user cannot access Docker.
prefix=clab-advnet-diamond
for node in r1 r2 r3 r4 host-a host-b; do
  if [[ $(docker inspect --format '{{.State.Running}}' "$prefix-$node") != true ]]; then
    echo "Container $prefix-$node is not running. Deploy labs/diamond.clab.yml first." >&2
    exit 1
  fi
done

echo 'Waiting up to 120 seconds for bidirectional data-network connectivity...'
deadline=$((SECONDS + 120))
until docker exec "$prefix-host-a" ping -I eth1 -c 1 -W 2 10.10.2.10 >/dev/null 2>&1 &&
      docker exec "$prefix-host-b" ping -I eth1 -c 1 -W 2 10.10.1.10 >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo 'Connectivity did not converge. Router diagnostics follow:' >&2
    for node in r1 r2 r3 r4; do
      echo "--- $node ---"
      docker exec "$prefix-$node" Cli -c 'show ip interface brief' || true
      docker exec "$prefix-$node" Cli -c 'show ip ospf neighbor' || true
      docker exec "$prefix-$node" Cli -c 'show ip route' || true
    done
    exit 1
  fi
  sleep 2
done

for node in r1 r2 r3 r4; do
  echo "--- $node OSPF neighbors ---"
  docker exec "$prefix-$node" Cli -c 'show ip ospf neighbor'
done

echo '--- Host A routes (remote subnet must use eth1) ---'
docker exec "$prefix-host-a" ip route
echo '--- Host B routes (remote subnet must use eth1) ---'
docker exec "$prefix-host-b" ip route
echo '--- Host A to Host B ---'
docker exec "$prefix-host-a" ping -I eth1 -c 5 -W 2 10.10.2.10
echo '--- Host B to Host A ---'
docker exec "$prefix-host-b" ping -I eth1 -c 5 -W 2 10.10.1.10
echo '--- Forwarding path from Host A ---'
docker exec "$prefix-host-a" traceroute -n -m 8 -w 1 10.10.2.10
echo 'PASS: packets crossed the routed data network in both directions.'
