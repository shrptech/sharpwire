#!/usr/bin/env python3
"""Q3: armed rollback over REST as the service account.
modes:  fire   - apply 3 changes, arm rollback at now+N, let it fire, verify revert
        error  - same, but the first inverse op's object is already gone (op fails), others must still run
        disarm - arm at now+N, disarm (remove scheduler) before it is due, verify nothing reverted
        arm    - apply + arm at now+N and exit (for the reboot test; check later with `verify`)
        verify - print current state + scheduler + sw-rb log lines
usage: q3_rollback.py MODE [N_seconds]"""
import httpx, json, sys, time
H = "192.168.88.1"; mode = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
sw = httpx.Client(base_url=f"https://{H}/rest", auth=("sharpwire", "Sw-Spike-1"), verify=False, timeout=60)
def req(m, p, b=None, show=True):
    r = sw.request(m, p, json=b)
    if show: print(f"{m} {p} {json.dumps(b) if b else ''} -> {r.status_code} {r.text[:160]}")
    return r
def ex(s): return sw.post("/execute", json={"script": s, "as-string": ""}).json().get("ret", "")
def one(p, **q):
    l = sw.get(p, params=q).json(); return l[0] if l else None
RUN = "sw-rb-run1"
ROLLBACK = r'''
:log info "sw-rb: start"
:do { /routing bgp connection remove rb-test } on-error={ :log warning "sw-rb: op1 (remove bgp connection rb-test) failed" }
:do { /routing filter rule remove [find chain="rb-chain" comment="sw:rb:1"] } on-error={ :log warning "sw-rb: op2 failed" }
:do { /ip address set [find address="10.250.0.9/24" interface="ether2"] address=10.250.0.1/24 } on-error={ :log warning "sw-rb: op3 failed" }
:do { /system scheduler remove [find name="sw-rb-run1"] } on-error={}
:log info "sw-rb: done"
'''
def state():
    c = one("/routing/bgp/connection", name="rb-test"); r = one("/routing/filter/rule", chain="rb-chain")
    a = [x["address"] for x in sw.get("/ip/address", params={"interface": "ether2"}).json()]
    return {"bgp rb-test": bool(c), "rule rb-chain": bool(r), "ether2 v4": a}
def baseline():
    for p, q in (("/routing/bgp/connection", {"name": "rb-test"}), ("/routing/filter/rule", {"chain": "rb-chain"}),
                 ("/system/scheduler", {"name": RUN}), ("/system/script", {"name": RUN}), ("/ip/address", {"interface": "ether2"})):
        for o in sw.get(p, params=q).json(): sw.delete(f"{p}/{o['.id']}")
    req("PUT", "/ip/address", {"address": "10.250.0.1/24", "interface": "ether2"}, show=False)
def apply():
    req("PUT", "/routing/bgp/connection", {"name": "rb-test", "instance": "core", "afi": "ip", "local.role": "ebgp",
        "local.address": "10.250.0.1", "remote.address": "10.250.0.0/24", "remote.as": "4200000100"})
    req("PUT", "/routing/filter/rule", {"chain": "rb-chain", "rule": "reject", "comment": "sw:rb:1"})
    a = one("/ip/address", address="10.250.0.1/24", interface="ether2")
    req("PATCH", f"/ip/address/{a['.id']}", {"address": "10.250.0.9/24"})
def arm(n):
    now = ex(":put [/system clock get time]"); date = ex(":put [/system clock get date]")
    h, m, s = map(int, now.split(":")); t = h * 3600 + m * 60 + s + n
    st = f"{t//3600%24:02d}:{t//60%60:02d}:{t%60:02d}"
    req("PUT", "/system/script", {"name": RUN, "source": ROLLBACK, "comment": "sharpwire rollback"})
    req("PUT", "/system/scheduler", {"name": RUN, "start-date": date, "start-time": st, "interval": "0s",
        "on-event": f"/system script run {RUN}", "comment": "sharpwire rollback"})
    print(f"armed: clock {date} {now} -> start {st}")
def verify():
    print("state:", state())
    k = one("/system/scheduler", name=RUN)
    print("scheduler:", {x: k.get(x) for x in ("name", "start-date", "start-time", "interval", "run-count", "next-run", "disabled", "owner", "policy")} if k else None)
    print("clock:", ex(":put [/system clock get date]"), ex(":put [/system clock get time]"), "uptime", sw.get("/system/resource").json()["uptime"])
    print("log:\n" + ex(':foreach i in=[/log find where message~"sw-rb"] do={:put ([/log get $i time]." ".[/log get $i message])}'))
if mode in ("fire", "error", "disarm", "arm"):
    baseline(); print("baseline:", state()); apply(); print("applied:", state()); arm(N)
    if mode == "error":
        c = one("/routing/bgp/connection", name="rb-test"); req("DELETE", f"/routing/bgp/connection/{c['.id']}")
    if mode == "disarm":
        k = one("/system/scheduler", name=RUN); req("DELETE", f"/system/scheduler/{k['.id']}")
    if mode != "arm":
        time.sleep(N + 20); verify()
elif mode == "verify":
    verify()
