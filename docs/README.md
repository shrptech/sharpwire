# sharpwire design docs

Nov 20, 2026 · Draft

sharpwire keeps the network matching the intent in NetBox: MikroTik cores and leafs, Proxmox VE nodes and SDN, and (in phase 2) the VyOS EVPN gateways. It renders full declarative config from NetBox, diffs it against live state, and applies approved plans with a rollback armed on the target before anything changes.

The network these docs build on is described in [network/proxmox-evpn-ipv6-underlay.md](network/proxmox-evpn-ipv6-underlay.md) ("the network doc"). Where the two disagree, the network doc wins and these docs need fixing.

## Documents

| # | Doc | Covers |
| --- | --- | --- |
| 1 | [Overview and context](01-overview.md) | Goals, phases, device roles, architecture |
| 2 | [Intent model](02-intent-model.md) | What lives in NetBox core vs the plugin, numbering, protected objects |
| 3 | [Pipeline and validation](03-pipeline-and-validation.md) | Build → validate → render → fetch → diff → plan, cross-device consistency rules |
| 4 | [Drivers](04-drivers.md) | RouterOS (cores, leafs), Proxmox (SDN, node files, cluster files), VyOS (phase 2) |
| 5 | [Apply and rollout](05-apply-and-rollout.md) | Armed rollback per driver, baseline-relative health checks, rollout order, node adoption, change sets |
| 6 | [Drift, approvals and change classes](06-drift-approvals-change-classes.md) | Drift handling, `public-vnet`, `customer-onboard`, freezes, sharpstack API |
| 7 | [Security and observability](07-security-and-observability.md) | Users, credentials, device accounts, alerting, metrics, audit |
| 8 | [Testing, build plan and risks](08-testing-build-plan-risks.md) | Labs, build order, risks, open questions |
| 9 | [sharpstack integration](09-sharpstack-integration.md) | Ownership, public VNet handoff, sharpstack prerequisites, reuse, phase 2 VPC mapping |

## Phases

| Phase | Network scope | sharpwire scope |
| --- | --- | --- |
| **1: Public network** | CRS317 leaf pair, CCR2116 cores (underlay, public handoff on VLAN 300, upstream), Proxmox nodes and the `public` EVPN zone | RouterOS and Proxmox drivers, drift, apply, the `public-vnet` change class |
| **2: VPC** | Per-customer NAT zones (`cNN`), VyOS gateway pair, VLAN 310 NAT outside on the cores | VyOS driver, per-customer zones on Proxmox, the `customer-onboard` change class |

Sections that only apply to phase 2 are marked **(phase 2)**.

## v1 scope (MVP)

v1 is the smallest useful slice of phase 1. Everything marked **(later)** in these docs is designed for, so the v1 data model and APIs leave room for it, but it isn't built in v1.

| In v1 | Later (designed, not built) |
| --- | --- |
| Drift detection on every phase-1 target: cores, leafs, Proxmox SDN, node files, cluster files, and the cluster firewall `enable` | Modelling and applying cluster and host firewall rules |
| Public-zone isolation cross-check (cluster, VM and NIC firewall), raised as a critical alert | |
| Apply to cores, Proxmox SDN and Proxmox node files, with armed rollback and health checks | Apply to leafs (read-only drift in v1) |
| | Apply to cluster files: corosync, `datacenter.cfg` (read-only drift in v1) |
| One plan per target, approved and applied separately; the operator follows the documented order for multi-target changes | Multi-target change sets with staged rollout. Plans already carry a `change_set_id` |
| Every plan approved by a human in a minimal web UI (Kanidm OIDC) | Change classes (`public-vnet`, `customer-onboard`) and the sharpstack API. Plans already carry a `change_class` field |
| Alerts stored in Postgres and shown in the web UI | Alertmanager (paging), chat notifications, Prometheus metrics |
| | Freezes. Every apply already passes one gate where the freeze check plugs in |
| Credentials from deploy-time secret files | Envelope-encrypted credentials in Postgres with a set and rotate API |

In v1, sharpstack integrates only through NetBox. An admin creates public capacity in NetBox, a human approves the plan, and sharpstack uses a VNet only once sharpwire marks it `active`. sharpstack must first stop writing SDN itself (see the [sharpstack prerequisites](09-sharpstack-integration.md#sharpstack-prerequisites-for-sharpwire-v1)).

## Decisions recorded so far

| Decision | Choice |
| --- | --- |
| Proxmox | sharpwire manages it: SDN via the PVE API, plus per-node host networking, `frr.conf.local` and cluster files. This replaces the network doc's Ansible and "script it against the PVE API" suggestions |
| Proxmox rollback | Armed on the node with a systemd timer, plus a boot-time restore so a reboot can't strand a bad config |
| Phase 1 auto-apply **(later)** | `public-vnet`: add-only VNet and subnets in the public zone |
| Phase 2 auto-apply | `customer-onboard`: add-only, covering the full per-customer block on Proxmox and VyOS from a fixed template |
| Adding a Proxmox node | Rendered by sharpwire and always approved by a human. Not a change class |
| VyOS failover | BGP preference (network doc B.4). conntrack-sync is optional |
| RouterOS EVPN | Not used. The cores carry no EVPN, and RouterOS is not an EVPN gateway |
| Upstream BGP | Managed in full by sharpwire; the ISP sessions are protected |
| Corosync | link1 over the VTEP loopbacks at low priority, added in phase 1 |
| Public VNets | Tags 200–999, named `vnetNNN` |
| Node access | Restricted sudo helper, installed by a sharpwire bootstrap package |
| Rollback timer | RouterOS 5 min, Proxmox 10 min |
| Reconcile interval | 5 minutes |
| Freezes **(later)** | Absolute. No break-glass |
| SDN ownership | sharpwire is the only SDN writer. sharpstack drops its SDN calls and VNI allocation (a v1 prerequisite) |
| Public VNets | Shared platform capacity, not per customer. sharpstack links a plugin VNet once it is `active` |
| Cluster PVE firewall | sharpwire owns `enable` (drift-only in v1) and, later, the cluster and host rules. Per-VM firewall and ipsets stay with sharpstack |
| Stack | sharpstack's baseline (Python 3.14, FastAPI, async SQLAlchemy, Alembic, ruff, mypy, uv, NetBox 4.7), but a Postgres job queue instead of Celery and a server-rendered UI |
| Alerting in v1 | Postgres `alerts` table shown in the web UI. No Alertmanager |
| v1 scope | A lean MVP (see [v1 scope](#v1-scope-mvp)) |
| Starting point | Greenfield: devices start from a minimal hand bootstrap and sharpwire builds the rest; no running device needs adopting (see [Greenfield bring-up](05-apply-and-rollout.md#greenfield-bring-up)) |

