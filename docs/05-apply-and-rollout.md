# 5. Apply and rollout

Nov 20, 2026 · Draft

Every push that can affect the management path arms a rollback on the target before changing anything, so a lost session reverts itself.

## Rollback timer

The armed rollback fires at T+N unless it is disarmed first. N is configurable per target type:

| Target | T+N | Why |
| --- | --- | --- |
| RouterOS | 5 min | REST ops and health checks finish in well under a minute |
| Proxmox node | 10 min | `ifreload`, FRR reload, BGP and BFD convergence, and the full node health-check set |
| Corosync (node-local restore) **(later)** | 10 min | Same as Proxmox nodes |
| VyOS **(phase 2)** | 5 min (commit-confirm) | |

If health checks are still running when T+N is reached, the rollback fires, and the run is recorded as failed.

## Stale check

At apply time the controller re-fetches live state for every target in the plan. If a fingerprint changed, the plan is stale: its approval is void, and the recomputed plan needs a fresh approval. For the Proxmox cluster target, the fingerprint covers both the running and the pending SDN config.

## Apply per driver

### RouterOS

RouterOS has no candidate config and no commit-confirm.

1. Take an `/export` snapshot.
2. Upload a rollback script built from the plan's inverse ops, and schedule it at T+N minutes. Each inverse op runs under its own `on-error`, so the script still restores everything it can after a partial apply.
3. Apply ops via REST in plan order.
4. Open a new session to prove reachability, then run health checks.
5. Disarm the scheduler.

### Proxmox nodes (host files)

Through the `sharpwire-node` helper:

1. Snapshot the managed files, **plus the generated `/etc/frr/frr.conf`**, into `/var/lib/sharpwire/rollback/<run-id>/`. FRR boots from the generated file, and pmxcfs isn't available before `networking.service` to regenerate it, so the boot-time restore puts back the generated file directly.
2. **Arm** the rollback in two parts:
   - write a marker file and start a transient systemd timer at T+N that restores the snapshot and reloads (`ifreload -a`, `sysctl --system`, the FRR reload);
   - make sure the persistent unit `sharpwire-restore.service` is enabled. It is ordered before `networking.service` and restores the snapshot at boot whenever the marker exists.
3. Write the files, run `ifreload -a -n`, then apply.
4. Open a **new** SSH session to prove reachability, then run health checks.
5. Disarm: stop the timer, then remove the marker.

The boot-time restore matters on Proxmox. A node that loses corosync while running HA resources self-fences and reboots, and a transient timer doesn't survive a reboot. Without the boot-time restore, the node would come back up on the broken config.

Before pushing to a node, the controller requires that the cluster is quorate, that no other node is mid-apply, and that no SDN apply is running.

### Proxmox cluster: SDN

SDN objects themselves can't cut the management path, because mgmt is static on `bond0.10`, outside SDN, and the hard guard keeps it that way. But an SDN apply also reloads networking on **every node at once**: it promotes any pending `/etc/network/interfaces.new`, runs `ifreload -a` on whatever `/etc/network/interfaces` holds, and reloads FRR. Anything unapplied on any node would go live everywhere, with no node-side rollback. (Spike: confirm exactly what PVE 9's SDN apply runs per node. The same per-node reload is probably the answer to the per-node FRR reload spike in [Drivers](04-drivers.md#node-targets-host-files).)

So an SDN apply first takes **every** node's lock, and it is refused while any node:
- has a pending `/etc/network/interfaces.new`;
- has `/etc/network/interfaces` that differs from the running state (the helper runs `ifquery --check -a`);
- has a managed file that differs from its rendered intent, or an armed rollback.

Under those preconditions, the per-node reload only re-applies what is already running. SDN rollback is therefore driven by the controller rather than armed on the target:

1. Snapshot the running SDN config.
2. Stage the ops, confirm the pending config equals the plan, then apply SDN.
3. Run health checks on every managed node, plus the post-merge FRR check.
4. On failure, stage the snapshot back, apply SDN again, and halt the rollout.

Discarding pending changes, and protecting the stage → compare → apply sequence against a concurrent writer, depend on PVE 9's SDN lock and rollback endpoints. Both are spike items.

SDN apply hits every node at once. That makes the pre-approval validation and the lab gate more important here than anywhere else (see [Risks](08-testing-build-plan-risks.md#risks)).

### Proxmox cluster: cluster files (later)

Read-only in v1; this describes the later apply path.

- **`datacenter.cfg`** is written through pmxcfs with no armed rollback, since it can't affect reachability.
- **`corosync.conf`** changes need a human and change one link at a time. The rollback is armed on **every** node: each node's helper snapshots its local `/etc/corosync/corosync.conf` and arms a timer that restores it and restarts corosync. This works even if the cluster has lost quorum, when `/etc/pve` is read-only. The mechanism is a spike item.

### VyOS (phase 2)

Apply via the HTTP API with commit-confirm, run health checks, then confirm. If the API on the running version lacks commit-confirm, use the RouterOS pattern.

## Health checks

Health checks compare against the target's state **before** the apply, rather than requiring every session to be up. Otherwise, bringing up the public zone, adding a node, or any core change during an unrelated ISP outage would fail its own checks and roll back.

1. **Baseline.** Before applying, the controller records the target's checkable state: every session, BFD peer, route and link the checks below cover.
2. **Absolute checks.** The management path always has to pass: a new session works and the mgmt address, default route and gateway are unchanged. So does the post-merge FRR check.
3. **No regressions.** Nothing that was up in the baseline may go down, except what the plan itself removes.
4. **Expected up.** Sessions and routes the plan adds must come up, but only when their far end is already configured in live state. Sessions whose far end is a later step are recorded as **expected down**. The operator checks them at the end of the procedure (in v1 by running the target's checks again from the UI; later, the change set does it).
5. **Pre-existing failures** (down in the baseline and not touched by the plan) are reported but don't fail the apply.

The table lists what is checked for each target. Steps 1–5 decide how each check counts.

| Target | Health checks |
| --- | --- |
| Core | API reachable; upstream sessions; an underlay session Established with BFD up for every managed node; node /128s installed with the node's VLAN address as gateway (third-party next hop); VLAN 300 sessions: one per node per family; public subnets installed with multiple gateways (warn only); **(phase 2)** NAT sessions to both gateways |
| Leaf | API reachable; MLAG peer up; every bond member up; bridge VLAN membership as rendered |
| Proxmox node | A new SSH session works; the only host default is IPv6 via mgmt and there's no IPv4 default; both underlay sessions Established; two next hops to every other node loopback; BFD peers up; every EVPN peer Established; `vrf_public` (once the public zone exists): 4 sessions with exactly one default received per family; post-merge FRR check; corosync quorate, every configured link up, and no token loss during the step |
| Proxmox cluster (SDN) | SDN apply status OK on every node, including nodes that have joined the cluster but aren't managed yet. Their node-level checks are skipped; `show evpn vni` lists the expected VNIs on every node; **(phase 2)** each `vrf_cNN` has v4 and v6 defaults via the gateway VTEPs |
| Gateway **(phase 2)** | API reachable; underlay sessions to both cores; EVPN sessions to every node and to the other gateway; each customer VRF holds its type-5 subnets from the nodes; outside sessions to both cores; conntrack-sync healthy **if enabled** |

These are the network doc's Step 7 checks that a machine can run. Checks 13 and 14 (VM data plane and customer isolation) need guests. Phase 2 replaces check 14 with a routing-table isolation check: `vrf_cNN` holds only its own prefixes and a default, and each customer VRF on the gateways holds only its veth default and its own type-5 routes.

## Rollout

Each target has a lock: a session-level Postgres advisory lock (`pg_advisory_lock`) held on the apply worker's own connection for the whole apply, and released automatically if the worker dies. sharpstack's transaction-scoped `advisory_xact_lock` doesn't fit, because it would hold a transaction open for up to the 10-minute rollback timer. Locks are taken in a fixed order, following sharpstack's lock-ordering rule. A redundancy group rolls out one member at a time, and the next member starts only after the previous one passes health checks. A failure halts the rollout and leaves the remaining members untouched.

- **Leafs and cores:** never both in one step.
- **Proxmox nodes:** one node at a time, in a stable order, waiting for corosync to settle between nodes.
- **Proxmox cluster:** a single step by nature. It is never in the same stage as node-file changes.
- **Gateways (phase 2):** the standby first, then the active.

### Change set stages

In v1 there are no change sets. The table below is the order the operator applies the separate per-target plans in, and it's the order the later change-set engine automates. Stages that touch leafs or cluster files are done by hand in v1.

Within a change set, stages run in a fixed order, and each stage rolls out serially as above. The order is driven by dependencies: L2 before L3, the far end of a session before the near end, and the gateway before the zones that depend on it.

| Change | Stages |
| --- | --- |
| **`public-vnet`** (phase 1) | 1. Cluster SDN: add the VNet and subnets, apply |
| **Create the public zone** (one-off bring-up) | 0. Leafs: VLAN 300 on the node and core bonds and the peer-link (by hand in v1) · 1. Cores: VLAN 300, the `pub4`/`pub6` connections and filters (the node sessions are expected down) · 2. Cluster SDN: the `public` zone, apply · 3. Nodes: `bond0.300` and the `vrf_public` FRR sessions, one node at a time · 4. Run the core checks again: one VLAN 300 session per node per family |
| **Add a Proxmox node** | see below |
| **`customer-onboard`** (phase 2) | 1. Gateways: the customer block, standby then active · 2. Cluster SDN: zone `cNN`, VNets and subnets, apply · 3. Isolation and default-route checks |
| **Customer offboarding** (phase 2, human) | The reverse: cluster SDN first, then the gateways |

### Adding a Proxmox node

A node can't be managed until it is reachable, so adding one is split around a manual bootstrap:

1. **Leaf ports (human-approved).** Add the node, its cabling and its bond (`mlag-id`) in NetBox. sharpwire pushes the leaf bonds and VLAN membership, leaf by leaf. **In v1** the leaf ports are configured by hand, and sharpwire's leaf drift check confirms they match NetBox.
2. **Manual bootstrap.** Install PVE, configure static mgmt on `bond0.10`, run the sharpwire bootstrap package, and join the cluster with `pvecm add … --link0 <mgmt>`. link1 for the node is added in step 4, once the node's loopback is reachable over the underlay.
3. **Adopt the node.** Mark the node managed in NetBox. Its first plan is an **adoption plan**:
   - The guarded mgmt values in NetBox (address, gateway, VLAN, bond members) must equal what the node is running. If they don't, validation tells the operator to fix NetBox; sharpwire never "corrects" a node's mgmt to match NetBox.
   - Other differences in `bond0`/`bond0.10` from the hand bootstrap, such as a missing `egress-qos-map`, are protected changes that need an admin override.
4. **Change set B (human-approved).** sharpwire stages:
   1. cores: the underlay connection and `ul-in-*` chain, core A then core B (the session to the new node is expected down);
   2. cluster SDN: the controller peer list, apply. The new node is a cluster member, so this generates its `interfaces.d/sdn`, `vrf_public` and `frr.conf` before its own files reference them;
   3. the new node: all of its host files (underlay, `bond0.300`, `frr.conf.local`, `/etc/hosts`). The core sessions and EVPN peers now have to come up;
   4. existing nodes: `/etc/hosts`, one node at a time;
   5. cluster files: the node's corosync link1 (needs its own approval because corosync is protected; done by hand in v1);
   6. run the core and SDN checks again;
   7. **(phase 2)** gateways: the new EVPN neighbor, standby then active.

Removing a node runs these stages in reverse and is always human-approved.

## Greenfield bring-up

The network is built from scratch, so no running device has to be adopted. Each device starts from a minimal hand-applied **bootstrap**, and sharpwire builds everything else. The bootstrap's mgmt values must equal NetBox; that is the only thing the first plan checks rather than changes.

| Device | Bootstrap (by hand) | Then |
| --- | --- | --- |
| Leafs | Full config by hand in v1, following the network doc's Appendix A.1 | sharpwire's drift check must show zero diff before anything else continues |
| Cores | RouterOS with no default config (`/system reset-configuration no-defaults=yes`), the VLAN 10 mgmt interface and address, the sharpwire service account, and REST over HTTPS | sharpwire's first plan builds the rest: underlay, VLAN 300, upstream, VRRP, firewall |
| Proxmox nodes | PVE install, static mgmt on `bond0.10`, the bootstrap package; the first node creates the cluster and the others join with `--link0 <mgmt>` | The [adoption plan](#adding-a-proxmox-node), then the node's host files |
| Cluster | Corosync link1 by hand (v1) | SDN controller, then the public zone ([stages](#change-set-stages)) |

The driver baseline for a core is exactly this bootstrap: the mgmt interface, its address and the service account are never removed.

## Crash recovery

A single controller instance is safe because rollback lives on the target, except for SDN, which is controller-driven. On startup:

1. Any run left in `applying` is marked interrupted.
2. The controller re-fetches live state for its targets and records whether a rollback fired. On nodes this comes from the helper's restore log; on RouterOS, from the scheduler state.
3. For a cluster target with pending SDN changes from the interrupted run, the controller discards them, and if SDN had already been applied it restores the snapshot.
4. The controller re-plans. It never resumes a run.
