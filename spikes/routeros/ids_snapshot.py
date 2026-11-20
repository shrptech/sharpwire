#!/usr/bin/env python3
"""Print natural-key -> .id maps (to compare before/after reboot). usage: ids_snapshot.py > file"""
import httpx, json
a = httpx.Client(base_url="https://192.168.88.1/rest", auth=("sharpwire", "Sw-Spike-1"), verify=False)
out = {}
for p, k in (("/interface", "name"), ("/ipv6/address", "address"), ("/routing/filter/rule", "comment"),
             ("/routing/bgp/connection", "name"), ("/system/scheduler", "name"), ("/system/script", "name")):
    out[p] = {o.get(k, "?"): o[".id"] for o in a.get(p).json()}
print(json.dumps(out, sort_keys=True))
