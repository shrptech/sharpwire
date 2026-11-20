# 5. Apply and rollout

Nov 20, 2026 · Draft

Every push that can affect the management path arms a rollback on the target before changing anything, so a lost session reverts itself. The rollback fires at T+N unless it's disarmed first: **5 minutes on RouterOS, 10 minutes on Proxmox nodes** (time for `ifreload`, the FRR reload and convergence). If health checks are still running at T+N, the rollback fires and the run is recorded as failed.

## Stale check

At apply time the controller re-fetches live state. If the fingerprint changed, the plan is stale: its approval is void and the recomputed plan needs a fresh one. On the cluster target, the fingerprint covers both the running and the pending SDN config.

## RouterOS

RouterOS has no candidate config and no commit-confirm.

1. Take an export: `POST /rest/execute {"script":"/export","as-string":""}` returns it in `ret`.
2. Upload a rollback script built from the plan's inverse ops. Each op runs in its own `:do { } on-error={ }`, so it restores what it can after a partial apply. When it runs, the script removes both scheduler entries and leaves a marker (a comment).
3. Arm **two** scheduler entries: one-shot at T+N, and `start-time=startup`. A one-shot whose time passes while the router is down never fires; the `startup` entry covers a reboot.
4. Apply the ops via REST in plan order.
5. Open a new session to prove reachability, then run health checks.
6. Disarm by removing both entries.

Whether a rollback fired is read from the marker, never from `run-count`, which resets at boot. All of this was verified on RouterOS 7.25.1 (`spikes/routeros/FINDINGS.md`).

## Proxmox nodes

Through the `sharpwire-node` helper:

1. Snapshot the managed files **and the generated `/etc/frr/frr.conf`** to `/var/lib/sharpwire/rollback/<run>/`. FRR boots from the generated file, and nothing can regenerate it early at boot.
2. **Arm:** write the marker atomically and sync it, then start a transient systemd timer at T+N that restores the snapshot and reloads. The persistent `sharpwire-restore.service` restores the snapshot at boot whenever the marker exists.
3. Write the files, validate, and apply ([Drivers](04-drivers.md#node-targets-host-files)).
4. Open a **new** SSH session to prove reachability, then run health checks.
5. Disarm: stop the timer, then remove the marker.

The boot-time restore matters because a node that loses corosync while running HA resources fences itself and reboots, and a transient timer doesn't survive that. `sharpwire-restore.service` was tested through a reboot and a hard power-off (`spikes/node-linux/`):
- `DefaultDependencies=no`, `RequiresMountsFor=/var/lib/sharpwire`, `ConditionPathExists=/var/lib/sharpwire/armed`, `WantedBy=sysinit.target`;
- `After=local-fs.target systemd-sysctl.service pvenetcommit.service`;
- `Before=network-pre.target networking.service frr.service pve-sdn-commit.service pve-cluster.service corosync.service`;
- it re-runs `systemd-sysctl`, deletes `interfaces.new`, creates `/run/network`, removes the marker, and logs to the journal.

Before pushing to a node, the controller requires a quorate cluster, no other node mid-apply, and no SDN apply running.

## Proxmox cluster: SDN

SDN objects can't touch the management path, because mgmt is static on `bond0.10` outside SDN. But an SDN apply reloads networking and FRR on **every node**: it promotes any `interfaces.new` and runs `ifreload -a` on whatever each node's file holds. So an SDN apply takes every node's lock, and is refused while any node:
- has a pending `interfaces.new`;
- has a running state that differs from its files: `ifquery --check -a`, plus the helper's default-route and extra-interface checks;
- has a managed file that differs from its rendered intent, or an armed rollback.

Under those conditions the per-node reload only re-applies what's already running. That's why the SDN rollback is driven by the controller rather than armed on the target:

1. Take the SDN lock and snapshot the running SDN config.
2. Stage, compare and apply ([Drivers](04-drivers.md#cluster-target-sdn)).
3. Check every node's reload task, then run health checks on every managed node, including the post-merge check.
4. On failure, stage the snapshot back and apply again. PVE commits before reloading, so rolling back always takes a second apply. Subnets left in PVE IPAM are cleaned up by the next plan.
5. Release the lock.

## Health checks

1. **Baseline.** Before applying, record the checks below.
2. **Must pass:** the management path (a new session works; the mgmt address, gateway and default route are unchanged) and, on nodes, the post-merge check.
3. **No regressions:** nothing up in the baseline may go down, except what the plan removes.
4. **Everything else is reported, not gated.** New sessions the plan adds, and failures that were already there before the apply, appear in the run's result. They don't trigger the rollback. The operator checks new sessions at the end of a procedure.

| Target | Checked |
| --- | --- |
| Core | API; an underlay session with BFD per node; node /128s via the node's own address on the VLAN, which is its link-local (`fe80::…%vlan101`); one VLAN 300 session per node per family; upstream sessions |
| Leaf | API; MLAG peer; bond members; bridge VLANs as rendered |
| Proxmox node | New SSH session; IPv6 default via mgmt only; both underlay sessions; two next hops to every other loopback; BFD peers; every EVPN peer; the `vrf_public` sessions with one default per family; corosync quorate, every link up, no token loss |
| Cluster (SDN) | Every node's reload task OK; `show evpn vni` lists the expected VNIs on every node |

## Rollout

Each target has a lock: a session-level Postgres advisory lock (`pg_advisory_lock`) on the apply worker's own connection, released automatically if the worker dies. Locks are taken in a fixed order. Redundant members roll out one at a time: core A, then core B; nodes one by one, waiting for corosync to settle. A failure halts the rollout and leaves the rest untouched. The cluster target never runs in the same step as a node.

## v1 procedures

In v1 every target gets its own plan, and the operator applies them in this order. [Change sets](later.md#change-sets) will automate the order later.

**Bring up the public zone** (once):
1. leafs: VLAN 300 on the node bonds, the core bonds and the peer-link (by hand);
2. cores: VLAN 300, the `pub4`/`pub6` connections and filters;
3. cluster SDN: zone `public`;
4. nodes: `bond0.300` and the `vrf_public` FRR sessions, one node at a time;
5. re-run the core checks: one VLAN 300 session per node per family.

**Add public capacity:** cluster SDN only: the VNet and its subnets.

**Add a Proxmox node:**
1. NetBox: the node, its cabling, its bond with `mlag-id`, its addresses, ASN and router-id. Configure the leaf ports by hand; the leaf drift check confirms them.
2. Bootstrap by hand: install PVE, set static mgmt on `bond0.10`, run the bootstrap package, and join with `pvecm add … --link0 <mgmt>`.
3. Mark the node managed. Its first plan must not change the mgmt values the hard guard covers. If NetBox and the node disagree there, fix NetBox. Any other bootstrap differences show up as [plan warnings](02-intent-model.md#plan-warnings).
4. Cores: the node's chain and connection, core A then core B.
5. Cluster SDN: the controller's peer list. This generates the new node's `vrf_public` and `frr.conf` before its own files need them.
6. The new node's host files.
7. The other nodes' `/etc/hosts`, one at a time.
8. Corosync link1 for the node, by hand.
9. Re-run the core and SDN checks.

Removing a node runs these steps in reverse.

## Greenfield bring-up

Each device starts from a minimal hand bootstrap whose mgmt values match NetBox:

| Device | Bootstrap (by hand) | Then |
| --- | --- | --- |
| Leafs | Full config, per network doc A.1 and the [rules](02-intent-model.md#routeros) | The drift check shows zero diff |
| Cores | `/system reset-configuration no-defaults=yes`, the mgmt VLAN interface and address, the service account, REST over HTTPS. Upstream, VRRP and the firewall by hand | sharpwire builds the underlay and the public handoff |
| Proxmox nodes | PVE, static mgmt, the bootstrap package, cluster create or join | Steps 3–9 of adding a node |
| Cluster | Corosync link1 | The SDN controller, then the public zone |

## Crash recovery

On startup:
1. Runs left in `applying` are marked interrupted.
2. Live state is re-fetched, and whether a rollback fired is read from the helper's restore log or the RouterOS marker.
3. On the cluster: force-release the SDN lock and `POST /cluster/sdn/rollback`. If the apply had already gone through, restore the snapshot with a second apply.
4. Re-plan. A run is never resumed.
