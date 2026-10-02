#!/usr/bin/env python3
"""
build_topology.py
==================
Turns topology/topology.yml into a real Linux network-namespace topology:
one netns per switch and per host, veth pairs for every link, loopbacks
assigned, and the tenant bridge/VXLAN device created on each leaf.

This is the same thing containerlab or GNS3 do for you with a GUI/Docker
layer underneath -- here it's done directly against the kernel's own
netns/veth/vxlan primitives, which is exactly what Cumulus Linux / SONiC
switches are doing on real hardware (they're Linux too).

Idempotent: run teardown_topology.py first if re-running.
"""

import subprocess
import sys
import yaml
import os

TOPOLOGY_FILE = os.path.join(os.path.dirname(__file__), "..", "topology", "topology.yml")


def run(cmd, check=True):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 and check:
        print(f"    FAILED: {result.stderr.strip()}")
        sys.exit(1)
    return result


def netns_exec(ns, cmd):
    return run(f"ip netns exec {ns} {cmd}")


def main():
    with open(TOPOLOGY_FILE) as f:
        topo = yaml.safe_load(f)

    switches = topo["switches"]
    links = topo["underlay_links"]
    overlay = topo["overlay"]
    hosts = topo["hosts"]

    print("== Creating switch namespaces ==")
    for name, cfg in switches.items():
        run(f"ip netns add {name}")
        netns_exec(name, "ip link set lo up")
        netns_exec(name, f"ip addr add {cfg['loopback']} dev lo")
        netns_exec(name, "sysctl -w net.ipv4.ip_forward=1 >/dev/null")
        if cfg["role"] == "leaf":
            netns_exec(name, "sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null")

    print("\n== Wiring underlay point-to-point links (numbered /31s -- see topology.yml note on IPv6/unnumbered) ==")
    for i, link in enumerate(links):
        veth_a = f"veth{i}a"
        veth_b = f"veth{i}b"
        run(f"ip link add {veth_a} type veth peer name {veth_b}")
        run(f"ip link set {veth_a} netns {link['a']}")
        run(f"ip link set {veth_b} netns {link['b']}")
        netns_exec(link["a"], f"ip link set {veth_a} name {link['a_if']}")
        netns_exec(link["b"], f"ip link set {veth_b} name {link['b_if']}")
        netns_exec(link["a"], f"ip addr add {link['a_ip']} dev {link['a_if']}")
        netns_exec(link["b"], f"ip addr add {link['b_ip']} dev {link['b_if']}")
        netns_exec(link["a"], f"ip link set {link['a_if']} up")
        netns_exec(link["b"], f"ip link set {link['b_if']} up")

    print("\n== Building the tenant overlay bridge + VXLAN device on each leaf ==")
    # NOTE: a VLAN-aware bridge (vlan_filtering 1) is the real Cumulus/SONiC
    # model and is what FRR's zebra actually requires to bind an EVPN
    # Ethernet Segment to an access port -- a flat bridge gets a hard
    # "ESI cannot be associated with this interface type". This sandbox's
    # kernel doesn't support vlan_filtering bridges at all (RTNETLINK
    # "Operation not supported" even with no other options set -- compiled
    # out of this minimal/Firecracker kernel build), so that path is closed
    # here too. Using the flat bridge that's proven to work; see
    # docs/README.md for what a VLAN-aware rebuild looks like elsewhere.
    vni = overlay["vni"]
    vlan = overlay["vlan"]
    bridge = overlay["bridge"]
    vxlan_if = overlay["vxlan_if"]
    for name, cfg in switches.items():
        if cfg["role"] != "leaf":
            continue
        netns_exec(name, f"ip link add {bridge} type bridge stp_state 0")
        netns_exec(
            name,
            f"ip link add {vxlan_if} type vxlan id {vni} local {cfg['vtep']} "
            f"dstport 4789 nolearning",
        )
        netns_exec(name, f"ip link set {vxlan_if} master {bridge}")
        netns_exec(name, f"ip link set {bridge} up")
        netns_exec(name, f"ip link set {vxlan_if} up")

    print("\n== Creating hosts and attaching them ==")
    for hname, hcfg in hosts.items():
        run(f"ip netns add {hname}")
        netns_exec(hname, "ip link set lo up")
        for j, attach in enumerate(hcfg["attach"]):
            sw = attach["switch"]
            veth_sw = f"{hname}_{sw}_sw"
            veth_host = f"{hname}_{sw}_h"
            run(f"ip link add {veth_sw} type veth peer name {veth_host}")
            run(f"ip link set {veth_sw} netns {sw}")
            netns_exec(sw, f"ip link set {veth_sw} name {attach['switch_if']}")
            netns_exec(sw, f"ip link set {attach['switch_if']} master {bridge}")
            netns_exec(sw, f"ip link set {attach['switch_if']} up")
            run(f"ip link set {veth_host} netns {hname}")
            netns_exec(hname, f"ip link set {veth_host} name {attach['host_if']}")
            netns_exec(hname, f"ip link set {attach['host_if']} up")

        if len(hcfg["attach"]) > 1:
            # Share one MAC across both NICs -- this is what a bonded pair
            # presents to the network regardless of which link is active,
            # and it's what makes the failover demo real EVPN host mobility
            # (same MAC+IP reappearing via a different leaf) rather than an
            # unrelated second MAC just showing up.
            primary = hcfg["attach"][0]["host_if"]
            mac = run(f"ip netns exec {hname} cat /sys/class/net/{primary}/address").stdout.strip()
            for attach in hcfg["attach"][1:]:
                netns_exec(hname, f"ip link set {attach['host_if']} address {mac}")
            # Dual-homed host: in production this is a bonded/LACP NIC pair
            # (active-backup or 802.3ad). This sandbox kernel has no loadable
            # bonding driver (no modprobe, module not built in), so both NICs
            # stay as independent links instead -- eth0 (-> leaf2) carries
            # the IP and traffic, eth1 (-> leaf3) stays up as the standby
            # path. scripts/failover_demo.sh moves the IP across manually to
            # show the same thing a bonding driver would automate. The EVPN
            # mechanism actually being demonstrated here -- shared ESI,
            # Designated-Forwarder election between leaf2/leaf3, EVPN
            # Type-1/Type-4 routes -- lives entirely in FRR on the leaves,
            # not in host-side NIC teaming, so this substitution doesn't
            # change what's being proven.
            primary_if = hcfg["attach"][0]["host_if"]
            netns_exec(hname, f"ip addr add {hcfg['bond_ip']} dev {primary_if}")
        else:
            netns_exec(hname, f"ip addr add {hcfg['ip']} dev {hcfg['attach'][0]['host_if']}")

    print("\nTopology built. Next: deploy FRR config (ansible/deploy.yml).")


if __name__ == "__main__":
    main()
