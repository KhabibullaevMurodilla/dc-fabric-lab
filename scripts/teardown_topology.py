#!/usr/bin/env python3
"""Deletes every namespace this lab creates, cleanly, so build_topology.py can rerun."""
import subprocess
import yaml
import os

TOPOLOGY_FILE = os.path.join(os.path.dirname(__file__), "..", "topology", "topology.yml")

with open(TOPOLOGY_FILE) as f:
    topo = yaml.safe_load(f)

names = list(topo["switches"].keys()) + list(topo["hosts"].keys())
for name in names:
    subprocess.run(f"ip netns del {name}", shell=True, capture_output=True)
print(f"Removed namespaces: {', '.join(names)}")
