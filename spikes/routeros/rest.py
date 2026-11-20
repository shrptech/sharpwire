#!/usr/bin/env python3
"""Tiny RouterOS REST client for the spike (python3 + httpx).
Usage: rest.py METHOD /path ['{json}']   env: RHOST (default 192.168.88.1), RUSER, RPASS
Prints the request line, HTTP status and the JSON body (pretty)."""
import json, os, sys, httpx
m, path = sys.argv[1].upper(), sys.argv[2]
body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
host = os.environ.get("RHOST", "192.168.88.1")
auth = (os.environ.get("RUSER", "admin"), os.environ.get("RPASS", "Spike-Adm1n!"))
r = httpx.request(m, f"https://{host}/rest{path}", json=body, auth=auth, verify=False, timeout=120)
print(f"> {m} /rest{path} {json.dumps(body) if body is not None else ''}".rstrip())
print(f"< {r.status_code}")
keys = [k for k in os.environ.get("RKEYS", "").split(",") if k]  # optional output filter
def flt(o): return {k: v for k, v in o.items() if k in keys} if keys and isinstance(o, dict) else o
try:
    j = r.json(); j = [flt(x) for x in j] if isinstance(j, list) else flt(j)
    print(json.dumps(j) if keys else json.dumps(j, indent=1))
except Exception: print(r.text[:2000])
