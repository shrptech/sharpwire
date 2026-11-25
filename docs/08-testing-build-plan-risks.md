# 8. Testing, build plan and risks

Nov 20, 2026 · Draft

## Testing

- **Unit and golden tests.**
  - Each driver's output is compared with golden files rendered from NetBox-shaped fixtures; the first goldens came from the network doc's examples, in the builder's canonical order (`tests/golden/`).
  - Every cross-target rule has a failing fixture.
  - Proxmox's merge is tested too: the rendered `frr.conf.local` goes through PVE's `append_local_config` and FRR templates, as in `spikes/node-linux/pve-merge/`, and the result is compared with a golden file.
  - The intent builder is tested from a GraphQL snapshot captured from NetBox 4.7.5 with the plugin, seeded with the network doc's example (`tests/fixtures/netbox/`). It must build exactly the hand-written example network, and render the same golden files.
- **Containerlab in CI.** Two CHRs act as cores, and FRR containers as nodes running the rendered `frr.conf.local`. Every driver change must pass:
  - render → apply → re-read gives an empty diff;
  - a withheld disarm makes the target revert;
  - the topology works: underlay sessions, next hops passed through (never via a core), ECMP, `vrf_public` receiving only defaults, and no /32 leaked to the cores;
  - two FRR reloads in a row reset no session.
- **Nested Proxmox lab.** Three nested PVE VMs, nightly and before releases. It covers SDN apply with the lock, the merge on PVE's own FRR 10.6.1, `ifreload`, the boot-time restore after a forced reboot, and corosync. sharpstack's integration suite runs against it too.
- **Physical lab.** CRS hardware for MLAG, LACP, `l2mtu` on SFP+ and VRRP. RouterOS and PVE upgrades are validated here before a pinned version moves.

## Build plan

Drift detection ships first, read-only, so the diff engine is proven before sharpwire writes anything.

0. **sharpstack prerequisites**, in sharpstack, in parallel with steps 1–4, and finished before step 6 ([sharpstack integration](09-sharpstack-integration.md#sharpstack-prerequisites-for-sharpwire-v1)).
1. **Spikes: done** (`spikes/`). Still open, for the labs:
   - the SDN apply end to end, including what a single node's failure looks like and re-staging after `/rollback`;
   - the merge on FRR 10.6.1;
   - overlay BFD at default timers during a core failure;
   - corosync and HA fencing with the boot-time restore;
   - BFD from the cores to FRR on non-listening connections;
   - `l2mtu`, LACP and MLAG on hardware.
2. **Intent builder and NetBox plugin:**
   - the two plugin models, the custom fields, roles and tags ([Intent model](02-intent-model.md)): **done** (`netbox-plugin/`, NetBox 4.7);
   - the GraphQL builder: **done** (`app/intent/builder.py`, `app/services/netbox.py`);
   - the node bootstrap package, with the helper's read and rollback commands: **done** (`node/`). Writing and applying the files comes with step 6, as a helper upgrade.
3. **Read-only drivers** for RouterOS and Proxmox, with drift detection and alerts. No operator UI yet: plans and alerts are served by a read-only JSON API, and the UI comes with approvals in step 5.
   - 3a. The plan model and the RouterOS diff, with its guards and warnings: **done** (`app/schemas/plan.py`, `app/drivers/routeros/`);
   - 3b. RouterOS live fetch over REST, with the field defaults checked against a CHR capture: **done** (`app/drivers/routeros/client.py`, `tests/fixtures/routeros/`). Leafs are tested from simulated device views until the hardware lab, since a CHR has no MLAG;
   - 3c. Targets, plans, alerts and the Postgres job queue; the 5-minute reconcile and the NetBox event rules; the JSON API: **done** (`app/models/`, `app/jobs/`, `app/api/`). It plans the cores and leafs;
   - 3d. Proxmox SDN render and read;
   - 3e. Node file renders, read through the helper over SSH (paramiko);
   - 3f. Cluster files and the cluster firewall's `enable` option.
4. **Cross-target validation**, run continuously against live state.
5. **Core apply:** plans, the stale check, approvals in the UI, armed rollback, serial rollout.
6. **Proxmox apply:** SDN first, then node files.
7. **Bring up the public zone** through sharpwire, in the lab and then in production.

What follows v1 is in [later](later.md).

## Risks

| Risk | Mitigation |
| --- | --- |
| A bad SDN apply hits every node, and PVE keeps going past failures | Validation; the lab gate; pending must equal the plan; per-node task checks; controller-driven restore |
| A second SDN writer (sharpstack, or a person in the GUI) | sharpstack's SDN calls and rights removed; pending changes block applies; drift alerts |
| A host-networking push cuts mgmt and corosync, and HA fencing reboots the node onto the broken config | The armed timer plus the boot-time restore; the mgmt hard guard; one node at a time; IPMI |
| The `frr.conf.local` merge drops a line, or a duplicated setting resets BGP sessions cluster-wide on every apply | The `frr.conf.local` rules; the post-merge and reload-stability checks; golden tests of the merge |
| The SDN lock has no expiry, and `pve-sdn-commit` applies pending changes at boot | Crash recovery force-releases the lock; staged changes never outlive one apply |
| RouterOS has no atomic commit; a one-shot rollback misses its time while the router is down; `/execute` returns 200 on errors | The rollback is armed before the first op, with a second `startup` entry; `ret` is checked every time; CI rollback tests |
| RouterOS REST and BGP behaviour change between 7.x releases | Pinned versions; hardware lab gate |
| A missing VLAN on the peer-link breaks node pairs without BFD noticing | The leaf VLAN rule, checked continuously against live state |
| The operator applies a multi-target procedure in the wrong order | Each plan's validation rejects references to things that don't exist yet; the stale check |
| Nothing pages in v1 | Acceptable while changes are human-driven; Alertmanager comes first [later](later.md#alerting-and-credentials) |

## Open questions

- [ ] Leaf STP priority and ICCP values, to be written into the [rules](02-intent-model.md#routeros) when the leafs are first configured.
- [ ] One operator identity: should sharpstack's platform admins also log in with Kanidm?
- [ ] A short CLI alias (`swire`?).

Phase 2 questions are in [later](later.md#phase-2-risks-and-open-questions).
