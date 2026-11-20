# sharpwire design docs

Nov 20, 2026 · Draft

sharpwire keeps the network matching the intent in NetBox: MikroTik cores and leafs, Proxmox VE nodes and SDN, and (in phase 2) VyOS gateways. It renders config from NetBox, diffs it against live state, and applies approved plans with a rollback armed on the target before anything changes.

The network is described in [network/proxmox-evpn-ipv6-underlay.md](network/proxmox-evpn-ipv6-underlay.md) (the network doc). For an overview of the network and sharpwire together, without config, read the [high-level design](network/hld.md). Where these docs disagree with it, the network doc wins and these docs need fixing.

**Principles:**
- **NetBox holds the full state.** Every value on a device comes from a NetBox object or a named rule ([Intent model](02-intent-model.md)).
- **v1 is as small as possible.** Everything that isn't v1 lives in [later](later.md), so docs 01–09 describe only what v1 does.

## Documents

| # | Doc | Covers |
| --- | --- | --- |
| 1 | [Overview](01-overview.md) | Goals, context, architecture |
| 2 | [Intent model](02-intent-model.md) | The NetBox mapping, named rules, plan warnings |
| 3 | [Pipeline and validation](03-pipeline-and-validation.md) | Pipeline, plan lifecycle, validation, hard guards |
| 4 | [Drivers](04-drivers.md) | RouterOS, Proxmox SDN, cluster and node files |
| 5 | [Apply and rollout](05-apply-and-rollout.md) | Armed rollback, health checks, v1 procedures, crash recovery |
| 6 | [Drift and approvals](06-drift-and-approvals.md) | Drift, approval, how sharpstack fits in |
| 7 | [Security and observability](07-security-and-observability.md) | Login, credentials, target accounts, alerts, audit |
| 8 | [Testing, build plan and risks](08-testing-build-plan-risks.md) | Labs, build order, risks, open questions |
| 9 | [sharpstack integration](09-sharpstack-integration.md) | Ownership, public VNet handoff, sharpstack prerequisites |
| — | [Later and phase 2](later.md) | Everything designed but not in v1 |

## Phases

| Phase | Scope |
| --- | --- |
| **1: Public network** (v1 is its minimal slice) | Leaf pair, cores (underlay and public handoff), Proxmox nodes and the `public` zone |
| **2: VPC** | Per-customer NAT zones, a VyOS gateway pair, NAT outside on the cores ([later](later.md#phase-2-vpc)) |

## v1 in one table

| In v1 | Not in v1 ([later](later.md)) |
| --- | --- |
| Drift detection on cores, leafs, SDN, node files, cluster files, and the cluster firewall `enable` | Apply to leafs, cluster files and firewall rules; the guest isolation check |
| Apply to cores (the rows sharpwire renders), SDN and node files, with armed rollback | Upstream, VRRP, firewall and system services on the cores, modelled in NetBox |
| One plan per target; the operator follows the documented order | Change sets |
| A person approves every plan in a minimal UI; one Kanidm group | Change classes, the sharpstack API, freezes, overrides and roles |
| Alerts in Postgres, shown in the UI | Alertmanager, chat, metrics |
| Credentials from secret files | Credentials in Postgres |

## Decisions

The single record of decisions. Other docs link here rather than restate them.

| Decision | Choice |
| --- | --- |
| Source of truth | NetBox: core models plus custom fields `asn`, `router_id`, `l2mtu`, `mlag_id`, `max_subnet_length`, `firewall_enabled`, `sharpwire_managed`, and plugin models Zone and VNet. Fixed shapes are named rules ([Intent model](02-intent-model.md)) |
| Starting point | Greenfield. Devices start from a minimal hand bootstrap |
| Proxmox | sharpwire manages SDN, node files and the cluster files; it is the only SDN writer |
| Proxmox rollback | A systemd timer on the node, plus a boot-time restore |
| RouterOS rollback | An inverse-op script with two schedulers (T+N and `startup`) |
| Rollback timer | RouterOS 5 min, Proxmox 10 min |
| Reconcile interval | 5 minutes |
| Node access | A restricted sudo helper, installed and upgraded by a bootstrap package |
| Approval | A person approves every plan. Hard guards can't be overridden; other risky changes are plan warnings |
| Core scope in v1 | The rows sharpwire renders; other rows are reported, never removed |
| Public VNets | Shared platform capacity; tags 200–999 named `vnetNNN`; sharpwire sets `status=active`, and sharpstack uses only `active` VNets |
| Cores | No EVPN. `nexthop-choice=propagate` towards the nodes |
| Node FRR | Router-id via `dummy0`'s MAC; no BFD profiles (FRR default timers); `advertise-subnets 0` on zone `public` |
| Corosync | link1 over the VTEP loopbacks at low priority, added by hand in v1 |
| Alerting | A Postgres `alerts` table shown in the UI |
| Stack | sharpstack's baseline, but a Postgres job queue and a server-rendered UI |
| Phase 2 | VyOS gateways with BGP-preference failover; `customer-onboard` auto-apply ([later](later.md#phase-2-vpc)) |
