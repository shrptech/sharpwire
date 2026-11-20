#!/usr/bin/python3
# Render merged.json with Proxmox's own FRR jinja templates (proxmox-ve-rs/proxmox-frr-templates),
# wrapped like PVE::Network::SDN::Frr::raw_config_to_string(). jinja2 stands in for minijinja.
import json
import sys

import jinja2

tpl_dir, js = sys.argv[1], json.load(open(sys.argv[2]))
frr = js["frr"]
for rm in frr.get("routemaps", {}).values():          # Rust struct defaults
    for e in rm: e.setdefault("matches", []); e.setdefault("sets", [])
env = jinja2.Environment(undefined=jinja2.ChainableUndefined, loader=jinja2.FileSystemLoader(tpl_dir), trim_blocks=True, lstrip_blocks=True)
env.filters.setdefault("items", lambda d: (d or {}).items())
body = env.get_template("frr.conf.jinja").render(**frr)
print("frr version 10.6.1\nfrr defaults datacenter\nhostname pve1\nlog syslog informational\nservice integrated-vtysh-config")
print(body.rstrip("\n")); print("!\nline vty\n!")
if js["dropped"]:
    sys.stderr.write("DROPPED BY MERGE (not rendered):\n" + "\n".join(js["dropped"]) + "\n")
