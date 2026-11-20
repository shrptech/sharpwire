#!/usr/bin/env python3
"""Apply tests/golden/routeros/core-a.rsc line by line via POST /rest/execute (as-string) and report
each line's result; then print the resulting export so silent changes can be diffed.
Substitution: sfp-sfpplus1/2 -> ether2/ether3 (CHR has no SFP ports).
Usage: golden_apply.py <rsc> [host]"""
import httpx, json, sys
rsc, H = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "192.168.88.3")
a = httpx.Client(base_url=f"https://{H}/rest", auth=("admin", "Spike-Adm1n!"), verify=False, timeout=60)
menu = ""
for n, line in enumerate(open(rsc).read().splitlines(), 1):
    l = line.strip().replace("sfp-sfpplus1", "ether2").replace("sfp-sfpplus2", "ether3")
    if not l or l.startswith("#"): continue
    if l.startswith("/"): menu = l; continue
    cmd = f"{menu} {l}"
    r = a.post("/execute", json={"script": cmd, "as-string": ""})
    try: body = r.json()
    except Exception: body = r.text
    print(f"L{n:<3} {r.status_code} {json.dumps(body)[:300]}")
print("\n==== export ====")
print(a.post("/execute", json={"script": "/export terse", "as-string": ""}).json()["ret"].replace("\r", ""))
