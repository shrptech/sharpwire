# 1. Overview and context

Nov 20, 2026 · Draft

sharpwire keeps the network matching the intent in NetBox. It renders config from NetBox, diffs it against live state, and applies approved plans with a rollback armed on the target.

## v1 goals

- Detect drift on every phase-1 target (cores, leafs, Proxmox SDN, node files, cluster files, the cluster firewall `enable`) and raise it for a person.
- Push config from NetBox to the CCR cores, Proxmox SDN and the Proxmox node files.
- Never lock a device out: arm a rollback before any change that can affect the management path.
- Roll out one device at a time, with health checks between steps.
- A person approves every plan, in a minimal web UI.

**Non-goals:**
- zero-touch provisioning;
- firmware and PVE upgrades;
- backups beyond pre-push snapshots;
- guests, storage, Ceph and HA groups;
- PVE users and ACLs;
- the per-VM firewall (sharpstack's);
- vendors other than RouterOS and Proxmox VE (VyOS comes in phase 2);
- controller high availability;
- RouterOS as an EVPN speaker.

## Context

The network is described in full in the [network doc](network/proxmox-evpn-ipv6-underlay.md). In phase 1, sharpwire manages:

| Role | Hardware | Function |
| --- | --- | --- |
| Core | 2× CCR2116 | Underlay eBGP to every node (one underlay VLAN per core), the public handoff on VLAN 300; hand-configured in v1: upstream eBGP, VRRP for the mgmt gateway, firewall. **No EVPN** |
| Leaf | 2× CRS317 | L2-only MLAG pair. Read-only in v1 |
| Proxmox node | PVE 9.x | VTEP; underlay eBGP to both cores; anycast gateway; public handoff from `vrf_public` |
| Proxmox cluster | (cluster) | SDN; `corosync.conf` and `datacenter.cfg` (read-only in v1) |

Network facts the design relies on:
- **IPv6-only underlay and mgmt.** Mgmt is static on VLAN 10 and holds the only host default route.
- **Shared underlay VLANs.** All nodes plus one core on each. Every node has its own underlay ASN, presented with `local-as`.
- **Next hops passed through.** The cores pass the originating node's next hop on (`nexthop-choice=propagate`), so node-to-node VXLAN never transits a core.
- **The cores aren't in the overlay.** EVPN is a full iBGP mesh between node loopbacks.
- **Public zone.** One shared VRF (`vrf_public`) with public IPv4 and IPv6. Each node peers eBGP with both cores on VLAN 300 under one shared ASN, advertises the public subnets and learns defaults. Customers are isolated by the PVE firewall.
- **Out-of-band access** through IPMI and serial console; API and SSH run in-band on VLAN 10.
- **Scale:** 10–50 managed targets within a year.

## Architecture

sharpwire has three parts: a NetBox plugin (two models plus custom fields), one controller, and the controller's own Postgres. Only the controller talks to devices and to the PVE API.

```
  admin / sharpstack ──▶ NetBox (+ plugin) ──GraphQL, event rules──▶ controller ──▶ Postgres (plans, runs, alerts, audit)
                                                                        │
                                       ┌────────────────────┬───────────┴──────────┬────────────────┐
                                  RouterOS REST        PVE API (SDN,         SSH (helper)       web UI
                                 (cores, leafs)        cluster, firewall)    node files
```

The stack is Python: a Django plugin, FastAPI, and a Postgres job queue (`SELECT … FOR UPDATE SKIP LOCKED`), with no Redis or Celery. Conventions follow sharpstack ([sharpstack integration](09-sharpstack-integration.md#reused-from-sharpstack)).

## Targets

A **target** is anything planned against with its own lock and live state: each RouterOS device, each Proxmox node (its files), and the Proxmox cluster (SDN, the files in `/etc/pve`, and the firewall `enable`). The cluster is a single target because one API call or file write changes every node at once.
