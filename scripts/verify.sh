#!/bin/bash
# Verifies the fabric is up: underlay BGP, EVPN overlay sessions, VTEP
# discovery, and real host-to-host data-plane traffic across leaves.
set -e

echo "=== Underlay + EVPN BGP sessions (leaf1) ==="
vtysh -N leaf1 -c "show bgp summary" 2>&1 | grep -v "Can't open"

echo
echo "=== VNI / VTEP discovery (leaf1) ==="
vtysh -N leaf1 -c "show evpn vni" 2>&1 | grep -v "Can't open"

echo
echo "=== Underlay reachability: leaf1 -> leaf2, leaf3 loopbacks (via spines) ==="
ip netns exec leaf1 ping -c2 -W1 10.0.0.2
ip netns exec leaf1 ping -c2 -W1 10.0.0.3

echo
echo "=== Data plane: host1 (leaf1) -> host2 (leaf2/leaf3), different leaves, same VNI ==="
ip netns exec host1 ping -c3 -W1 10.100.100.12

echo
echo "=== leaf1's EVPN-learned FDB (remote MACs mapped to remote VTEPs) ==="
ip netns exec leaf1 bridge fdb show dev vxlan100 | grep -v "00:00:00:00:00:00"
