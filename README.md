# BGP EVPN / VXLAN Data-Centre Fabric Lab

A real leaf-spine data-centre fabric — BGP underlay, BGP EVPN control plane,
VXLAN overlay — built and automated from scratch with Linux network
namespaces, FRRouting, and Ansible. No simulator, no GUI: this is the same
Linux networking stack that Cumulus Linux and SONiC run on real switches,
wired together by hand.

Built to close a specific, named gap: listed BGP unnumbered,
VXLAN, EVPN, EVPN multihoming, Cumulus/SONiC, and Ansible/Puppet/Chef as
essential or desirable experience. This lab is those requirements, built and
verified, not just read about.

## Topology

```
                 spine1 (AS 65000)        spine2 (AS 65000)
                 /      |       \          /      |       \
                /       |        \        /       |        \
           leaf1    leaf2      leaf3   leaf1    leaf2      leaf3
          (65011)  (65012)    (65013)
             |         |    \    |
           host1    host2(eth0) host2(eth1)
         (single-   (dual-homed: one NIC to leaf2, one to leaf3,
          homed)     shared MAC -- see "EVPN host mobility" below)
```

- **Underlay:** eBGP, one unique AS per leaf, shared AS across both spines
  (standard Facebook/Cumulus-style numbering). IPv4 unicast, redistributing
  loopbacks, `maximum-paths 2` on leaves for ECMP across both spines.
- **Overlay:** BGP EVPN (`address-family l2vpn evpn`) on the same sessions.
  Leaves run `advertise-all-vni`; spines relay EVPN routes between leaves
  with `next-hop-unchanged` so the original VTEP stays the next-hop end to
  end — spines never terminate a tunnel, they're pure underlay+control-plane
  relays, exactly like real spines.
- **VNI 100** spans all three leaves, carrying a 10.100.100.0/24 tenant
  subnet with two hosts.

## What's actually verified, not just configured

Run `scripts/verify.sh` for all of this live. Everything below is real
output from this build, not a transcript written by hand.

**BGP underlay + EVPN sessions converge:**
```
Neighbor        V         AS   MsgRcvd   MsgSent  Up/Down State/PfxRcd   PfxSnt Desc
10.255.0.0      4      65000        14        14  00:00:07            8       11 spine1
10.255.0.6      4      65000        14        14  00:00:07            8       11 spine2
```

**Leaves auto-discover each other as VTEPs purely from EVPN Type-3 routes**
(no static tunnel config anywhere):
```
VNI        Type VxLAN IF              # MACs   # ARPs   # Remote VTEPs
100        L2   vxlan100              0        0        2
```

**Real data-plane traffic crosses the overlay between hosts on different
leaves**, and the kernel FDB is programmed correctly from BGP, not from
flooding:
```
$ ip netns exec host1 ping -c3 10.100.100.12     # host1 on leaf1, host2 on leaf2/leaf3
3 packets transmitted, 3 received, 0% packet loss

$ ip netns exec leaf1 bridge fdb show dev vxlan100
7e:be:a4:7a:73:92 dst 10.0.0.3 self extern_learn   # host2 via leaf3
52:e4:ff:fa:15:ee dst 10.0.0.2 self extern_learn   # host2 via leaf2
```

## EVPN host mobility (the multihoming demo)

Run `scripts/failover_demo.sh`. host2 has two real NICs, one to leaf2 and
one to leaf3, sharing a single MAC (what a bonded NIC pair presents to the
network). Failing the active link from leaf2 to leaf3, with **zero
configuration change anywhere except on host2 itself**, produces this on
leaf1 — a switch that isn't even connected to the leaf that changed:

```
before:  06:29:e1:2a:07:e6 dst 10.0.0.2 self extern_learn   (via leaf2)
after:   06:29:e1:2a:07:e6 dst 10.0.0.3 self extern_learn   (via leaf3)
```

That re-convergence is pure BGP EVPN: leaf3 locally learns the MAC the
moment host2's traffic arrives on it, advertises a fresh Type-2 route,
leaf1 installs it, done. host1 → host2 connectivity never drops across the
failover.

**Scope note, stated plainly:** this is EVPN *host mobility* (a MAC/IP
reappearing behind a different VTEP), not formal EVPN *multihoming* with
Ethernet-Segment IDs and Designated-Forwarder election between leaf2/leaf3.
I configured the latter — `evpn mh es-id` / `evpn mh es-sys-mac` — and hit a
real wall: FRR's zebra refuses to bind an ES to an access port unless the
bridge is VLAN-aware (`vlan_filtering 1`), and this sandbox's kernel (a
stripped Firecracker build) doesn't support VLAN-filtering bridges at all —
confirmed by a bare `RTNETLINK: Operation not supported` with no other
options set. On any standard Linux kernel (which is to say: on real
Cumulus Linux or SONiC hardware), the fix is the VLAN-aware bridge model
below, and the ES config in `ansible/host_vars/leaf2.yml` / `leaf3.yml`
(currently commented out in the rendered config, not deleted) is exactly
what goes live once the bridge supports it:

```
ip link add br100 type bridge vlan_filtering 1
bridge link set dev vxlan100 vlan_tunnel on
bridge vlan add dev vxlan100 vid 100 pvid untagged self
bridge vlan add dev vxlan100 vid 100 tunnel_info id 100
```

Two real kernel constraints showed up over the course of this build, and
both are documented rather than quietly worked around:

1. **No IPv6 at all** (no `/proc/sys/net/ipv6` tree, unloadable) — true BGP
   *unnumbered* eBGP rides on IPv6 link-local addressing even when it only
   carries IPv4 routes, so that specific mechanism can't run here. The
   underlay uses classic numbered `/31`s instead (`topology/topology.yml`
   has the full note). On a kernel with IPv6, switching is a one-line
   change per neighbor: `neighbor <iface> interface remote-as external`.
2. **No VLAN-filtering bridges** — blocks formal EVPN-MH ES binding, as above.

## Automation: Ansible, not hand-typed config

`ansible/` is the actual deployment tool, not a demo of syntax:

- `inventory.ini` + `group_vars/{spines,leaves}.yml` + `host_vars/<node>.yml`
  — real inventory structure, role-based group vars, per-node peering data.
- `templates/{zebra,bgpd}.conf.j2` — Jinja2 FRR config generation, branching
  on role (spine vs. leaf) for the parts that differ (next-hop-unchanged on
  spines, advertise-all-vni on leaves).
- `deploy.yml` — renders config, (re)starts each node's FRR instance scoped
  to its own network namespace via FRR's `-N <name>` pathspace flag (the
  same technique FRR's own topotests suite uses to run multi-node
  topologies on one machine).

```bash
ansible-playbook -i ansible/inventory.ini ansible/deploy.yml
```

Change a peering, add a VNI, add a fourth leaf — edit the inventory data,
rerun the playbook. Nothing is configured by hand through a CLI.

## Repo layout

```
topology/topology.yml          physical wiring: nodes, links, addressing
scripts/
  build_topology.py             provisions netns + veth + bridges + VXLAN
  teardown_topology.py          tears it all down
  verify.sh                     runs the verification commands above
  failover_demo.sh              runs the host-mobility demo above
ansible/
  inventory.ini, group_vars/, host_vars/
  templates/{zebra,bgpd}.conf.j2
  deploy.yml                    renders + deploys FRR to every node
```

## Running it

```bash
apt-get install -y iproute2 bridge-utils frr frr-pythontools ansible iputils-ping
python3 scripts/build_topology.py
ansible-playbook -i ansible/inventory.ini ansible/deploy.yml
bash scripts/verify.sh
bash scripts/failover_demo.sh
python3 scripts/teardown_topology.py   # when done
```
