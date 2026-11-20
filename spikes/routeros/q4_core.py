#!/usr/bin/env python3
"""Q4/Q5/Q6: configure the core CHR via REST as the minimal service account (read,write,api,rest-api):
addresses on ether2, filter chains (Appendix A.2 syntax), explicit underlay connections ul-a/ul-b
(nexthop-choice=default) and the pub6 prefix listener. Prints every request + status."""
import httpx, json, sys
H = "192.168.88.1"
adm = httpx.Client(base_url=f"https://{H}/rest", auth=("admin", "Spike-Adm1n!"), verify=False, timeout=60)
if not adm.get("/user/group", params={"name": "sharpwire"}).json():
    adm.put("/user/group", json={"name": "sharpwire", "policy": "read,write,api,rest-api", "comment": "sharpwire service account"})
    adm.put("/user", json={"name": "sharpwire", "group": "sharpwire", "password": "Sw-Spike-1"})
sw = httpx.Client(base_url=f"https://{H}/rest", auth=("sharpwire", "Sw-Spike-1"), verify=False, timeout=60)
def put(path, body):
    r = sw.put(path, json=body); print(f"PUT {path} {json.dumps(body)} -> {r.status_code} {r.json().get('.id', r.json())}")
for a in ("2001:db8:101::1/64", "2001:db8:250::1/64"):
    put("/ipv6/address", {"address": a, "interface": "ether2", "advertise": "no"})
rules = [("ul-in-a", "if (dst == 2001:db8:ffff::11/128) { accept }"), ("ul-in-a", "reject"),
         ("ul-in-b", "if (dst == 2001:db8:ffff::12/128) { accept }"), ("ul-in-b", "reject"),
         ("ul-out", "if (dst in 2001:db8:ffff::/64 && dst-len == 128) { accept }"), ("ul-out", "reject"),
         ("pub-in", "if (dst in 198.51.100.0/24 && dst-len in 24-28) { accept }"),
         ("pub-in", "if (dst in 2001:db8:70::/48 && dst-len in 48-64) { accept }"), ("pub-in", "reject"),
         ("default-out", "if (dst == 0.0.0.0/0 || dst == ::/0) { accept }"), ("default-out", "reject")]
for i, (ch, rule) in enumerate(rules):
    put("/routing/filter/rule", {"chain": ch, "rule": rule, "comment": f"sw:{ch}:{i}"})
for n, peer, asn in (("ul-a", "2001:db8:101::11", "4200000011"), ("ul-b", "2001:db8:101::12", "4200000012")):
    put("/routing/bgp/connection", {"name": n, "instance": "core", "afi": "ipv6", "local.role": "ebgp",
        "local.address": "2001:db8:101::1", "remote.address": peer, "remote.as": asn, "use-bfd": "yes",
        "nexthop-choice": "default", "input.filter": f"ul-in-{n[-1]}", "output.filter-chain": "ul-out"})
put("/routing/bgp/connection", {"name": "pub6", "instance": "core", "afi": "ipv6", "local.role": "ebgp",
    "local.address": "2001:db8:250::1", "remote.address": "2001:db8:250::/64", "remote.as": "4200000100",
    "use-bfd": "yes", "input.filter": "pub-in", "output.filter-chain": "default-out",
    "output.default-originate": "always"})
