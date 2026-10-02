#!/bin/bash
# Demonstrates EVPN host mobility: host2 is dual-homed to leaf2 and leaf3
# with one shared MAC across both NICs (what a bonded pair presents to the
# network). Failing the active link over to the other leaf makes the same
# MAC+IP reappear via a different VTEP, and the rest of the fabric
# re-converges with zero manual config anywhere else -- purely from the
# EVPN Type-2 route BGP just propagated.
set -e

MAC=$(ip netns exec host2 cat /sys/class/net/eth0/address 2>/dev/null || \
      ip netns exec host2 cat /sys/class/net/eth1/address)

echo "=== leaf1's current view of host2's MAC ($MAC) ==="
ip netns exec leaf1 bridge fdb show dev vxlan100 | grep "$MAC" || echo "(not learned yet -- ping something from host2 first)"

ACTIVE_IF=$(ip netns exec host2 ip -o addr show | awk '/10.100.100.12/{print $2}')
if [ "$ACTIVE_IF" = "eth0" ]; then
  OLD=eth0; NEW=eth1; OLD_LEAF="leaf2 (10.0.0.2)"; NEW_LEAF="leaf3 (10.0.0.3)"
else
  OLD=eth1; NEW=eth0; OLD_LEAF="leaf3 (10.0.0.3)"; NEW_LEAF="leaf2 (10.0.0.2)"
fi

echo
echo "=== Failing host2 over: $OLD ($OLD_LEAF) -> $NEW ($NEW_LEAF) ==="
ip netns exec host2 ip addr del 10.100.100.12/24 dev $OLD
ip netns exec host2 ip link set $OLD down
ip netns exec host2 ip addr add 10.100.100.12/24 dev $NEW
ip netns exec host2 ip link set $NEW up   # don't assume it's already up -- not true after a prior failover
ip netns exec host2 ping -c2 -W1 10.100.100.11 >/dev/null  # generate traffic so the new leaf learns the MAC

sleep 2
echo
echo "=== leaf1's view after failover -- same MAC, new VTEP, no config changed on leaf1 ==="
ip netns exec leaf1 bridge fdb show dev vxlan100 | grep "$MAC"

echo
echo "=== host1 -> host2 still reachable, now via $NEW_LEAF ==="
ip netns exec host1 ping -c2 -W1 10.100.100.12
