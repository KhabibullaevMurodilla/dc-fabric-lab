#!/usr/bin/env python3
"""
generate_dashboard_data.py
============================
Captures the fabric's REAL, live state -- BGP underlay + EVPN sessions,
VNI/VTEP discovery, FDB entries, the host1<->host2 data-plane ping, and a
live run of the host-mobility failover demo -- and writes it as
web/data.json for the static dashboard at web/index.html to render.

Every number in web/data.json comes from an actual command run against
the actual namespaces/FRR daemons at generation time (vtysh's own JSON
output, `bridge -j fdb show`, real `ping`), the same discipline the rest
of this lab holds itself to in scripts/verify.sh. Nothing here is
fabricated or hand-typed.

Assumes the topology is already built and FRR already deployed (run
scripts/build_topology.py + ansible-playbook ansible/deploy.yml first --
or let .github/workflows/pages.yml do that for you, which is what it's
for). Run from the repo root:

    python3 scripts/generate_dashboard_data.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY_PATH = ROOT / "topology" / "topology.yml"
OUT_PATH = ROOT / "web" / "data.json"


def sh(cmd: list[str], timeout: int = 10) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout


def vtysh_json(node: str, command: str) -> dict:
    out = sh(["ip", "netns", "exec", node, "vtysh", "-N", node, "-c", command])
    # vtysh prints a harmless "can't open vtysh.conf" warning line before
    # the JSON when run with -N against a per-namespace instance; the JSON
    # itself always starts at the first '{'.
    start = out.find("{")
    if start == -1:
        return {}
    return json.loads(out[start:])


def load_topology() -> dict:
    import yaml

    return yaml.safe_load(TOPOLOGY_PATH.read_text())


def build_nodes_and_links(topo: dict) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": name, "role": cfg["role"], "asn": cfg["asn"], "loopback": cfg["loopback"].split("/")[0]}
        for name, cfg in topo["switches"].items()
    ]
    links = [
        {"a": l["a"], "a_if": l["a_if"], "b": l["b"], "b_if": l["b_if"]}
        for l in topo["underlay_links"]
    ]
    hosts = []
    for name, cfg in topo["hosts"].items():
        hosts.append({
            "id": name,
            "attach": [{"switch": a["switch"]} for a in cfg["attach"]],
            "ip": cfg.get("ip") or cfg.get("bond_ip"),
        })
    return nodes, links, hosts


def collect_bgp_state(node_names: list[str]) -> dict:
    state = {}
    for node in node_names:
        underlay = vtysh_json(node, "show bgp summary json")
        evpn = vtysh_json(node, "show bgp l2vpn evpn summary json")
        state[node] = {"underlay": underlay.get("ipv4Unicast", {}), "evpn": evpn}
    return state


def collect_vni_tables(leaf_names: list[str]) -> dict:
    return {leaf: vtysh_json(leaf, "show evpn vni json") for leaf in leaf_names}


def collect_fdb(leaf_names: list[str]) -> dict:
    fdb = {}
    for leaf in leaf_names:
        out = sh(["ip", "netns", "exec", leaf, "bridge", "-j", "fdb", "show", "dev", "vxlan100"])
        try:
            fdb[leaf] = json.loads(out)
        except json.JSONDecodeError:
            fdb[leaf] = []
    return fdb


def ping(src_ns: str, dst_ip: str, count: int = 2) -> dict:
    out = sh(["ip", "netns", "exec", src_ns, "ping", "-c", str(count), "-W", "2", dst_ip], timeout=15)
    transmitted = received = 0
    for line in out.splitlines():
        if "packets transmitted" in line:
            parts = line.split(",")
            transmitted = int(parts[0].strip().split()[0])
            received = int(parts[1].strip().split()[0])
    return {"transmitted": transmitted, "received": received, "loss_pct": (
        round(100 * (transmitted - received) / transmitted, 1) if transmitted else None
    )}


def run_failover_demo() -> dict:
    """Runs the real failover: fails host2's active NIC over to its other
    leaf and captures leaf1's FDB view of host2's MAC before and after,
    plus the data-plane ping, exactly like scripts/failover_demo.sh --
    reimplemented here so the before/after state can be captured as
    structured data rather than parsed back out of shell text.
    """
    mac = sh(["ip", "netns", "exec", "host2", "cat", "/sys/class/net/eth0/address"]).strip()

    def leaf1_view_of(mac: str) -> dict | None:
        fdb = json.loads(sh(["ip", "netns", "exec", "leaf1", "bridge", "-j", "fdb", "show", "dev", "vxlan100"]) or "[]")
        for entry in fdb:
            if entry.get("mac") == mac and entry.get("dst"):
                return {"mac": mac, "dst": entry["dst"]}
        return None

    before = leaf1_view_of(mac)

    # Find which interface actually holds the address right now -- `ip -o
    # -4 addr show` prints one line per address, with the interface name as
    # the second whitespace-separated field (e.g. "19: eth0    inet
    # 10.100.100.12/24 ..."), which is a reliable field to parse, unlike
    # searching for the literal substring "eth0" in the line (that method
    # previously always failed to match, since it compared the full line
    # remainder against the bare string "eth0").
    active_if = None
    for line in sh(["ip", "netns", "exec", "host2", "ip", "-o", "-4", "addr", "show"]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and "10.100.100.12" in line:
            active_if = fields[1].split("@")[0]
            break
    if active_if is None:
        raise RuntimeError("host2 has no interface currently holding 10.100.100.12 -- can't fail it over")

    new_if = "eth1" if active_if == "eth0" else "eth0"
    old_leaf, new_leaf = ("leaf2", "leaf3") if active_if == "eth0" else ("leaf3", "leaf2")

    subprocess.run(["ip", "netns", "exec", "host2", "ip", "addr", "del", "10.100.100.12/24", "dev", active_if], check=True)
    subprocess.run(["ip", "netns", "exec", "host2", "ip", "link", "set", active_if, "down"], check=True)
    subprocess.run(["ip", "netns", "exec", "host2", "ip", "addr", "add", "10.100.100.12/24", "dev", new_if], check=True)
    # Belt-and-suspenders: explicitly bring the target interface up rather
    # than assuming it already is -- true on a fresh build, but not after
    # an earlier failover already took the other interface down.
    subprocess.run(["ip", "netns", "exec", "host2", "ip", "link", "set", new_if, "up"], check=True)
    ping("host2", "10.100.100.11", count=2)  # generate traffic so the new leaf learns the MAC
    import time
    time.sleep(3)

    after = leaf1_view_of(mac)
    ping_after = ping("host1", "10.100.100.12", count=2)

    return {
        "mac": mac,
        "old_leaf": old_leaf, "new_leaf": new_leaf,
        "before": before, "after": after,
        "ping_host1_to_host2_after_failover": ping_after,
    }


def main() -> None:
    topo = load_topology()
    nodes, links, hosts = build_nodes_and_links(topo)
    node_names = [n["id"] for n in nodes]
    leaf_names = [n["id"] for n in nodes if n["role"] == "leaf"]

    # Warm-up: a host's MAC can't be EVPN-advertised until its own leaf's
    # bridge has actually seen a frame from it (the VXLAN devices here run
    # with `nolearning`, so dynamic data-plane learning is disabled by
    # design -- only BGP-pushed state reaches the FDB, and BGP only has
    # something to push once a host sends something). Without this, a
    # freshly-deployed, otherwise-silent host can take anywhere up to
    # ~60s to become reachable purely through flood-and-learn off someone
    # else's ARP retries, which would make CI flaky for no real reason.
    # One real packet from the host itself (not a simulated fact) makes
    # this deterministic instead of racy.
    print("Warming up: letting host1 and host2 each send one real packet so their MACs are known...", file=sys.stderr)
    subprocess.run(["ip", "netns", "exec", "host2", "ping", "-c", "1", "-W", "2", "10.100.100.11"], capture_output=True)
    subprocess.run(["ip", "netns", "exec", "host1", "ping", "-c", "1", "-W", "2", "10.100.100.12"], capture_output=True)
    import time
    time.sleep(3)

    print("Collecting BGP underlay + EVPN state...", file=sys.stderr)
    bgp_state = collect_bgp_state(node_names)

    print("Collecting VNI/VTEP tables...", file=sys.stderr)
    vni_tables = collect_vni_tables(leaf_names)

    print("Collecting FDB snapshots...", file=sys.stderr)
    fdb_before = collect_fdb(leaf_names)

    print("Running underlay reachability pings...", file=sys.stderr)
    underlay_pings = {
        f"{a}_to_{b}": ping(a, next(n["loopback"] for n in nodes if n["id"] == b))
        for a in ["leaf1"] for b in ["leaf2", "leaf3"]
    }

    print("Running host1 -> host2 data-plane ping...", file=sys.stderr)
    dataplane_ping = ping("host1", "10.100.100.12")

    print("Running the live failover demo...", file=sys.stderr)
    failover = run_failover_demo()

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "links": links,
        "hosts": hosts,
        "overlay": {"vni": topo["overlay"]["vni"], "subnet": topo["overlay"]["subnet"]},
        "bgp_state": bgp_state,
        "vni_tables": vni_tables,
        "fdb_before": fdb_before,
        "underlay_pings": underlay_pings,
        "dataplane_ping": dataplane_ping,
        "failover": failover,
    }

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
