# Spike: PVE 9 SDN apply, FRR merge, locks (source reading)

Nov 20, 2026. Source read only, nothing run. Shallow clones from git.proxmox.com:

| Repo | Commit | Version |
| --- | --- | --- |
| pve-network | 2ab23ef | libpve-network-perl 1.6.10 |
| pve-manager | 35d0d66 | 9.2.33 |
| pve-common | a1c4b90 | 9.2.12 |
| pve-cluster | 761b879 | 9.1.13 |
| pve-firewall | fd30caf | 6.0.8 |
| qemu-server | 5edab9d | 9.2.20 |

Path prefixes: `net:` = `pve-network/src/PVE/`, `mgr:` = `pve-manager/`.

## Summary

| # | Question | Verdict | Consequence |
| --- | --- | --- | --- |
| 1 | `PUT /cluster/sdn` end to end | partly | The config is committed first. Nodes are then reloaded one at a time, in unordered sequence, and the loop continues past failures. A failed node is invisible in the apply task, so check each node's `srvreload` task |
| 2 | Per-node work | confirmed, with caveats | The merge is structured but lossy: **`bfd` profiles, top-level `ip route` and `ip nht` are dropped**, and route-map sequence numbers are renumbered. If `frr-reload.py` fails, FRR is restarted |
| 3 | Reload one node only | confirmed | `pvesh set /nodes/<n>/network --regenerate-frr 1` (local, as root). It also promotes `interfaces.new` and runs `ifreload -a` |
| 4 | Pending, lock, rollback | confirmed (since 1.1.3, PVE 9.0) | Lock token and `/rollback` exist. The lock has no expiry, the boot-time `pve-sdn-commit` ignores it, and rollback doesn't undo IPAM writes |
| 5 | Privileges | partly | Writing the firewall `enable` option needs **Sys.Modify on /**. Reading a single zone or controller needs SDN.Allocate |
| 6 | EVPN, IPv6, router-id | partly | Peer group `VTEP`; no generated `update-source`. The IPv6 router-id comes from the `dummy0` MAC (random unless pinned). A local `bgp router-id` becomes a duplicate line |
| 7 | Mgmt-path exposure | confirmed | Every apply promotes `interfaces.new` and runs `ifreload -a` on every node |
| 8 | Other contradictions | — | `pvenetcommit.service` and `pve-sdn-commit.service` apply pending state at boot |

## 1. `PUT /cluster/sdn`

- The handler is `net:API2/Network/SDN.pm` `reload` (285–356) and needs SDN.Allocate on `/sdn` (292).
- Inside `lock_sdn_config` it calls `commit_config()` **synchronously** (`net:Network/SDN.pm` 241–248). That writes `/etc/pve/sdn/.running-config` with version+1. Only then does it fork the `reloadnetworkall` task. The new running config is therefore live before any node reloads, and it stays live if a node fails.
- The task loops over `get_nodelist()`, which returns `keys %$nodelist` (`pve-cluster src/PVE/Cluster.pm:311-321`). That order is unordered, and the list includes offline and unmanaged nodes.
- For each node, `$create_reload_network_worker` (110–132) runs `pvesh set /nodes/<n>/network --regenerate-frr 0|1`. For remote nodes, pvesh goes over SSH as `pvesh --noproxy` (`mgr:PVE/CLI/pvesh.pm` `proxy_handler` 111–135).
- pvesh runs in a CLI environment, so the remote `fork_worker` runs synchronously (`pve-common src/PVE/RESTEnvironment.pm:529`, wait at 736–747). **The reload is sequential, not parallel.**
- Reporting:
  - pvesh prints the UPID and exits 0 even when the node's task fails. Only SSH or pvesh errors become a `warn` (344), which ends the outer task as WARNINGS. Nothing halts on a failure.
  - To detect a failure, look at each node's `srvreload`/`networking` task: `GET /nodes/{n}/tasks?typefilter=srvreload&since=…`. These tasks are owned by root@pam, so reading them needs Sys.Audit on `/nodes/{n}` (`mgr:PVE/API2/Tasks.pm:184`).
  - Also read `/cluster/resources?type=sdn`. A zone shows `pending` when the `#version` in that node's `interfaces.d/sdn` lags the running config (`net:Network/SDN/Zones.pm` `status` 227–290).

## 2. Per-node work

`mgr:PVE/API2/Network.pm` `reload_network_config` (884–953) does the following, in order:

1. Renames `interfaces.new` to `interfaces` whenever the file exists (924).
2. Writes `interfaces.d/sdn` (`net:Network/SDN.pm` 538–542; a missing `source` line only produces a warning, 520), then the DHCP and WireGuard config.
3. Runs `ifreload -a` with no flags (938). `run_command` dies on a non-zero exit, so **FRR is not regenerated if `ifreload` fails**.
4. Calls `generate_frr_config(1)` (`SDN.pm` 593–611):
   - It rewrites `/etc/frr/daemons`, forcing `bgpd=yes`, `ospfd=no`, `fabricd=no` (`Frr.pm` 125–189).
   - It writes `/etc/frr/frr.conf` with a hard-coded `frr version 10.6.1` header (199–219).
   - `Frr::apply` (71–92) runs `systemctl enable --now frr` if FRR isn't enabled, then `/usr/lib/frr/frr-reload.py --stdout --reload /etc/frr/frr.conf`. **If the reload fails it falls back to `systemctl restart frr`.** It also restarts FRR whenever the daemons file changed.

The merge is `Frr::append_local_config` (`net:Network/SDN/Frr.pm` 245–495). It's a structured line parser, and Rust templates render the result (`PVE::RS::SDN::get_frr_raw_config`).

- **Merged blocks.** The body of a `router bgp <asn> [vrf X]` block that matches a generated router is **appended** to that router (309–334). `address-family` blocks merge into the matching generated family, or are added as new ones. `vrf`, `interface` and other `router` blocks are handled the same way.
- **Dropped lines.** Any other top-level line goes to `$frr_config->{''}`, which is never rendered (279, 460–461). The exceptions are `ip/ipv6 prefix-list … seq`, `access-list … seq`, `bgp community-list` and `route-map … N`. So `bfd`/`profile` blocks, `ip nht`, top-level `ip route` and prefix-lists without `seq` are **silently dropped**. Upstream's own test shows this: `src/test/zones/evpn/frr_local_merge/frr.conf.local` has `ip nht resolve-via-default` and `ip route 192.0.2.0/24 …`, and neither is in `expected_controller_config`.
- **Lines containing `!`** are dropped anywhere (456).
- **Route-maps** are never merged. Every entry is **renumbered 1..n per route-map name**, generated entries first (406–418, 471–494). `UL-IN permit 10` becomes `permit 1`.
- **Conflicts** are not detected. A second `bgp router-id`, or extra `neighbor VTEP …` lines, land after the generated ones in the same block (the upstream test output has a duplicate `neighbor VTEP remote-as`, lines 13–34). At boot FRR applies lines in order, so the last one wins. What `frr-reload.py` does with duplicates is **not verified**; it may re-apply the generated router-id and reset sessions.

## 3. One node only

There is no SDN-specific per-node endpoint and no `pvesdn` CLI.

- **Supported path:** `PUT /nodes/{node}/network` with `regenerate-frr=1`, which needs Sys.Modify on `/nodes/{node}` (`Network.pm` 889).
  - The helper runs it locally as root: `pvesh set /nodes/$(hostname)/network --regenerate-frr 1`.
  - It does everything in §2: `interfaces.new` promotion, `interfaces.d/sdn` from the **running** config (a no-op when current), `ifreload -a`, and the FRR reload.
  - It doesn't touch pending SDN changes.
  - It can be the helper's single apply step after writing `interfaces` and `frr.conf.local`. The helper must check the task status itself, because pvesh exits 0 regardless.
- **FRR only:** `perl -MPVE::Network::SDN -e 'PVE::Network::SDN::generate_frr_config(1)'`. Internal API, so gate it on the helper version.
- **Preview:** `GET /cluster/sdn/dry-run?node=` (`SDN.pm` 378–445, Sys.Audit on `/nodes/{node}`) returns the diff between the current `/etc/frr/frr.conf` and what would be generated from **pending** SDN plus the current `frr.conf.local`. Usable as a pre-flight check for both drivers.

## 4. Pending, lock, rollback

- **Where state lives.** Pending config is the section files `/etc/pve/sdn/*.cfg`; running config is the JSON in `.running-config` (`SDN.pm` 32). `?pending=1` diffs them per object and returns `state` (new/changed/deleted) plus a `pending` sub-object (139–193). `?running=1` returns the running config.
- **`POST /cluster/sdn/lock`** (134–171) returns a UUIDv7 token, stored in `/etc/pve/sdn/.lock`. It refuses while changes are pending unless `allow-pending=1`. While the lock exists, every SDN write, `PUT /cluster/sdn` and `/rollback` die without the matching `lock-token` (`lock_sdn_config` 407–427).
- **`DELETE /cluster/sdn/lock`** releases it, with the token or with `force=1` (173–216).
- **`POST /cluster/sdn/rollback`** (218–283) rewrites zones, vnets, subnets, controllers, fabrics, route-maps and prefix-lists from the running config, discarding **all** pending changes.
- **Lock release.** `PUT /cluster/sdn` and `/rollback` default to `release-lock=1`, so they **release the lock** unless told otherwise.
- All of this arrived in 1.1.3 (29 Jul 2025, PVE 9.0) and needs SDN.Allocate on `/sdn`.
- **Gaps:**
  - The lock has no expiry. A crashed controller blocks all SDN writes until someone uses `force`.
  - `mgr:bin/pve-sdn-commit` (run by `pve-sdn-commit.service` at boot) **commits any pending SDN changes** and reloads only that node (99–122), with no lock check.
  - Creating or updating a subnet writes to IPAM immediately, both the subnet and the gateway IP (`net:Network/SDN/SubnetPlugin.pm` 227–251). `/rollback` doesn't undo those writes.

## 5. Privileges

| Need | Evidence | Requirement |
| --- | --- | --- |
| Create, update, delete controllers, zones, VNets, subnets | `API2/Network/SDN/{Controllers,Zones,Vnets,Subnets}.pm` | SDN.Allocate on `/sdn/...`. Zones also need SDN.Use or SDN.Allocate on `/sdn/ipams/pve` and `/sdn/controllers/X` (`Zones.pm` 475–515). A propagated SDN.Allocate on `/sdn` covers all of these |
| Apply, lock, rollback | `SDN.pm` 141, 180, 225, 292 | SDN.Allocate on `/sdn` |
| Read pending config | `Controllers.pm` 197 | The index needs SDN.Audit or SDN.Allocate. **A single `GET` of a zone or controller needs SDN.Allocate** (`Zones.pm` 291, `Controllers.pm` 217) |
| Dry-run, per-node tasks | `SDN.pm` 383, `Tasks.pm` 184 | Sys.Audit on `/nodes/{n}` (covered by Sys.Audit on `/`) |
| Cluster firewall options | `pve-firewall …/Firewall/Cluster.pm` 91, 117 | Read: Sys.Audit on `/`. **Write: Sys.Modify on `/`** |
| VM config, VM firewall options | `qemu-server src/PVE/API2/Qemu.pm` 1948, `pve-firewall …/VM.pm` 74 | VM.Audit on `/vms/{vmid}` |

## 6. EVPN controller and IPv6

- **Peer group.** All in `net:Network/SDN/Controllers/EvpnPlugin.pm`. The name is `peer-group-name // 'VTEP'` (41–47, 221); the option exists since 1.5.1. Setting it also renames the route-maps to `MAP_VTEP_IN_<name>`/`MAP_VTEP_OUT_<name>`.
- **update-source** is generated only from the loopback of a BGP or IS-IS controller (260). We have neither, so the local `neighbor VTEP update-source dummy0` is required.
- **Router-id.** The local IP is found by matching `peers` against the parsed `/etc/network/interfaces` (`Zones/Plugin.pm` 329–367). For IPv6 the router-id is **the last 4 bytes of that interface's MAC** (`Controllers/Plugin.pm` 116–144). A dummy interface gets a random MAC unless `hwaddress` pins it.
- **IPv6-only underlay** was completed in 1.6.0 (18 May 2026): sorted peers, router-id derivation, and a check that peers are all v4 or all v6 (698). Test `ipv6underlay` shows `vxlan-local-tunnelip <v6>` and v6 neighbours. Its MTU is 1450, which assumes IPv4 overhead, so set the zone MTU explicitly.
- **Generated route-maps.** `MAP_VTEP_IN`/`MAP_VTEP_OUT` are generated as `permit 1` with no match. Local entries with the same names get renumbered behind it and never match. Use the controller options `route-map-in`/`route-map-out` (1.4.0) instead.

## 7. Mgmt-path exposure

Confirmed (§2, steps 1 and 3). Every apply promotes `interfaces.new` and runs `ifreload -a` on every node, whether or not the change is related, and even with nothing pending. The EVPN part of `interfaces.d/sdn` only adds vxlan, VNet bridge and VRF stanzas. Without exit nodes or VLAN zones, SDN itself doesn't touch `bond0`.

## 8. Other contradictions with the docs

1. `05` says SDN apply runs on "every node at once". It actually runs sequentially, unordered, and keeps going after a failure.
2. `pvenetcommit.service` moves `interfaces.new` into place at boot, before `sysinit.target`.
3. `pve-sdn-commit.service` commits pending SDN changes at boot (§4).
4. The `bfd` profiles in network doc Step 4a are dropped by the merge, so `bfd profile overlay` refers to a profile that no longer exists.
5. Route-map sequence numbers in `show run` won't match what we rendered.
6. Setting the router-id explicitly in `frr.conf.local` produces duplicate lines (§2).

## Recommended doc changes

- **04-drivers.md, "Cluster target: SDN" → Apply.** Lock with `POST /lock`, stage with `lock-token`, compare `?pending=1` and the per-node dry-run, then `PUT` with `release-lock=0`. Check each node's `srvreload` task, then release the lock. Crash recovery: `DELETE /lock force=1`, then `/rollback`.
- **04-drivers.md, "Node targets" → Apply.** Use `pvesh set /nodes/<self>/network --regenerate-frr 1` and check the task status. Pin `hwaddress` on `dummy0` and drop the local `bgp router-id`. Replace the BFD profiles with per-neighbour timers. The post-merge check should normalise route-map sequence numbers and assert that no rendered line is missing.
- **05-apply-and-rollout.md, "Proxmox cluster: SDN".** Describe the apply as sequential, unordered and non-halting. Because the commit happens before any reload, rollback means re-staging and re-applying. Note that rollback leaves IPAM entries behind.
- **05-apply-and-rollout.md, "Proxmox nodes".** Order `sharpwire-restore.service` with `After=pvenetcommit.service` and `Before=networking.service frr.service pve-sdn-commit.service`. The restore should remove `interfaces.new`. Keep the window between staging and applying short.
- **07-security-and-observability.md, PVE API row.** Add Sys.Modify on `/` (only when the firewall apply arrives; v1 is read-only).
- **08-testing-build-plan-risks.md.** Close the PVE spike items. Add risks: lines silently dropped by the merge; a failed reload leads to a full FRR restart; the lock has no expiry; pending changes get committed at boot. Add to the nested lab tests: `frr-reload.py` with a duplicate router-id, inline BFD timers on FRR 10.6, `bfdd` in `/etc/frr/daemons`, re-staging a subnet after rollback, and per-node task output when one node fails.

**Not confirmable from source:** how `frr-reload.py` handles duplicates, runtime FRR and ifupdown2 behaviour, and how long a sequential apply takes on a real cluster.
