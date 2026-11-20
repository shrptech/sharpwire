#!/usr/bin/env python3
"""Remove the Q2 test users/groups/scripts/schedulers/rules/connections/bridges (as admin)."""
import httpx, sys
H = sys.argv[1] if len(sys.argv) > 1 else "192.168.88.1"
a = httpx.Client(base_url=f"https://{H}/rest", auth=("admin", "Spike-Adm1n!"), verify=False)
for path, key, pfx in [("/system/scheduler", "name", ("k-", "ki-")), ("/system/script", "name", ("s-",)),
                       ("/user", "name", ("p-", "q-")), ("/user/group", "name", ("p-", "q-")),
                       ("/routing/bgp/connection", "name", ("c-",)), ("/routing/filter/rule", "chain", ("t-",)),
                       ("/interface/bridge", "name", ("rs-", "ri-"))]:
    for o in a.get(path).json():
        if o.get(key, "").startswith(pfx): a.delete(f"{path}/{o['.id']}")
print("cleaned")
