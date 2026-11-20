#!/usr/bin/env python3
"""Q2: which user-group policies does the sharpwire service account need?
For each candidate group: create group+user (as admin), then as that user try REST reads/writes,
create a /system/script + /system/scheduler (start in ~20s, both 'script run' and inline on-event),
/execute export; wait; check (as admin) whether the scheduled actions really ran."""
import httpx, json, os, sys, time
H = sys.argv[1] if len(sys.argv) > 1 else "192.168.88.1"
ADM = ("admin", "Spike-Adm1n!")
def c(auth): return httpx.Client(base_url=f"https://{H}/rest", auth=auth, verify=False, timeout=60)
adm = c(ADM)
CANDS = {
    "p-a": "read,write,rest-api",
    "p-b": "read,write,test,rest-api",
    "p-c": "read,write,policy,rest-api",
    "p-d": "read,write,policy,test,rest-api",
    "p-e": "read,write,policy,test,api,rest-api,sensitive",
    "p-f": "write,rest-api",
    "p-g": "read,write,policy,test,api",          # no rest-api
}
def short(r):
    try: j = r.json()
    except Exception: j = r.text
    if isinstance(j, dict): j = j.get("detail") or j.get("message") or j.get("ret", "")[:60] or "ok"
    elif isinstance(j, list): j = f"list[{len(j)}]"
    return f"{r.status_code} {j}"
clock = adm.post("/execute", json={"script": ":put [/system clock get time]", "as-string": ""}).json()["ret"]
date = adm.post("/execute", json={"script": ":put [/system clock get date]", "as-string": ""}).json()["ret"]
h, m, s = map(int, clock.split(":")); t = h * 3600 + m * 60 + s + 40
start = f"{t//3600%24:02d}:{t//60%60:02d}:{t%60:02d}"
if os.environ.get("CANDS"): CANDS = json.loads(os.environ["CANDS"])  # round 2: narrow the set
res = {}
for g, pol in CANDS.items():
    for path, key in (("/user", g), ("/user/group", g)):
        for o in adm.get(path, params={"name": key}).json(): adm.delete(f"{path}/{o['.id']}")
    adm.put("/user/group", json={"name": g, "policy": pol})
    adm.put("/user", json={"name": g, "group": g, "password": "Pw-" + g})
    u = c((g, "Pw-" + g)); r = {}
    r["GET resource"] = short(u.get("/system/resource"))
    r["PUT filter rule"] = short(u.put("/routing/filter/rule", json={"chain": "t-" + g, "rule": "reject"}))
    r["PUT bgp conn"] = short(u.put("/routing/bgp/connection", json={"name": "c-" + g, "instance": "core",
        "local.role": "ebgp", "remote.address": "2001:db8:99::1", "remote.as": "65001", "afi": "ipv6"}))
    r["PUT script"] = short(u.put("/system/script", json={"name": "s-" + g,
        "source": f"/interface bridge add name=rs-{g}"}))
    r["PUT sched(run script)"] = short(u.put("/system/scheduler", json={"name": "k-" + g, "start-date": date,
        "start-time": start, "interval": "0s", "on-event": f"/system script run s-{g}"}))
    r["PUT sched(inline)"] = short(u.put("/system/scheduler", json={"name": "ki-" + g, "start-date": date,
        "start-time": start, "interval": "0s", "on-event": f"/interface bridge add name=ri-{g}"}))
    r["exec export"] = short(u.post("/execute", json={"script": "/export", "as-string": ""}))
    res[g] = r
print("scheduled at", date, start, "- waiting 75s"); time.sleep(75)
for g in CANDS:
    names = {b["name"] for b in adm.get("/interface/bridge").json()}
    for k in adm.get("/system/scheduler", params={"name": "k-" + g}).json() + adm.get("/system/scheduler", params={"name": "ki-" + g}).json():
        res[g][f"{k['name'].split('-')[0]} owner/policy/run-count"] = f"{k.get('owner')} / {k.get('policy')} / {k.get('run-count')}"
    for k in adm.get("/system/script", params={"name": "s-" + g}).json():
        res[g]["script owner/policy"] = f"{k.get('owner')} / {k.get('policy')}"
    res[g]["ran: via script"] = f"rs-{g}" in names
    res[g]["ran: inline"] = f"ri-{g}" in names
for g, r in res.items():
    print(f"\n## {g}: {CANDS[g]}")
    for k, v in r.items(): print(f"  {k:32} {v}")
log = adm.post("/execute", json={"script": ":foreach i in=[/log find] do={:put ([/log get $i time].\" \".[/log get $i topics].\" \".[/log get $i message])}", "as-string": ""}).json()["ret"]
print("\n## log tail\n" + "\n".join(log.splitlines()[-25:]))
