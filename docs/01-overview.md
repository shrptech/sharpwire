# 1. Overview and context

Nov 20, 2026 · Draft

## Overview

sharpwire keeps the network matching the intent in NetBox. It renders full declarative config from NetBox, diffs it against live state, and applies approved plans with an automatic rollback armed on the target. It runs standalone and integrates with sharpstack through NetBox and an API.

**v1 goals (MVP, phase 1)**

- Detect drift on every phase-1 target (cores, leafs, Proxmox SDN, node files, cluster files, the cluster-level PVE firewall) and raise it for human approval
- Push config from NetBox intent to the CCR cores (RouterOS 7), Proxmox SDN and Proxmox node files (networking, sysctl, FRR, hosts)
- Never lock a device out: arm a rollback before any change that can affect the management path
- Roll out serially across the core pair and the Proxmox nodes, with health checks between steps
- Every plan approved by a human in a minimal web UI

**Later in phase 1** (designed, not built in v1; see the [v1 scope](README.md#v1-scope-mvp))

- Apply to the leafs and to the cluster files (corosync, `datacenter.cfg`)
- Multi-target change sets
- Change classes (`public-vnet`) and the sharpstack API
- Freezes and chat notifications
- Credential storage in Postgres

**Phase 2 goals (VPC)**

- Push config to the VyOS gateway pair
- Per-customer NAT zones end to end: Proxmox zone, VNets and subnets plus the VyOS customer block, auto-applied through `customer-onboard`

**Non-goals**

- Zero-touch provisioning: installing PVE, joining a node to the cluster, and first-boot device config stay manual (see [Adding a Proxmox node](05-apply-and-rollout.md#adding-a-proxmox-node))
- Firmware and PVE upgrades, and backups beyond pre-push snapshots
- Proxmox guests, storage, Ceph, HA groups, PVE users or ACLs, and the per-VM firewall and ipsets (all sharpstack's)
- Vendors other than RouterOS, VyOS and Proxmox VE
- Controller high availability
- RouterOS as an EVPN speaker or gateway

## Context

The network is described in full in the [network doc](network/proxmox-evpn-ipv6-underlay.md). sharpwire manages these roles:

| Role | Hardware | Phase | Function | Driver models |
| --- | --- | --- | --- | --- |
| Core | 2× MikroTik CCR2116 | 1 | Underlay eBGP to every node (VLAN 101 on core A, 102 on core B), public handoff on VLAN 300, upstream eBGP, mgmt gateway (VRRPv3 on VLAN 10). **No EVPN** | Interfaces, VLANs, bond, addresses, BGP instance and connections, routing filters, BFD, VRRP, firewall, system services |
| Core, NAT outside | same | 2 | eBGP to the VyOS pair on VLAN 310 | Additional BGP connections and filters |
| Leaf | 2× MikroTik CRS317 | 1 | L2-only MLAG pair | Ethernet (l2mtu), bonds with `mlag-id`, bridge, bridge VLANs, MLAG, mgmt VLAN, system services |
| Proxmox node | n× PVE 9.x | 1 | VTEP; underlay eBGP to both cores; anycast gateway; public-zone handoff from `vrf_public` | `/etc/network/interfaces`, sysctl, `/etc/frr/frr.conf.local`, `/etc/hosts` |
| Proxmox cluster | (cluster object) | 1 | SDN and cluster-wide config | SDN controller, zones, VNets, subnets; `corosync.conf`; `datacenter.cfg` |
| NAT gateway | 2× VyOS VM | 2 | VTEP, iBGP EVPN to every node, `outside` VRF, per-customer VRF, veth pair and SNAT; active/standby | Interfaces, VRFs, VXLAN, bridges, veths, BGP (underlay, EVPN, outside), policy, static routes, NAT, conntrack, firewall |

Network facts the design relies on:

- **IPv6-only underlay and management.** Hosts have no IPv4 outside tenant VRFs. Mgmt is static on VLAN 10 and holds the only host default route.
- **Shared underlay VLANs.** VLAN 101 (all nodes plus core A) and VLAN 102 (all nodes plus core B). Every node has a unique underlay ASN, presented with `local-as` from the single FRR process (ASN 65000).
- **Third-party next hop.** The cores re-advertise node /128s with the originating node's VLAN address as next hop, so node-to-node VXLAN never transits a core.
- **The cores are not in the overlay.** EVPN is a full iBGP mesh between node loopbacks (plus the VyOS VTEPs in phase 2). There is no route reflector.
- **Public zone.** One shared VRF (`vrf_public`, L3VNI 20000) with public IPv4 and IPv6. Each node peers eBGP from `vrf_public` to both cores on VLAN 300 with shared ASN 4200000100, advertises the public subnets and learns defaults. There are no exit nodes, VRRP or middleboxes. Customers sharing the zone are isolated by the Proxmox firewall.
- **VPC zones (phase 2).** One zone, VRF and L3VNI per customer (`cNN`, L3VNI `100NN`). Private IPv4 ranges may overlap between customers. Each customer gets one NAT IPv4 and a routed IPv6 /56. Traffic exits via VyOS.
- **Out-of-band access.** IPMI for the Proxmox nodes and serial console for the network devices. In-band API and SSH run over VLAN 10, without a management VRF.
- **Scale.** 3+ Proxmox nodes now, 10–50 managed targets within a year.

## Architecture

sharpwire has three parts: a NetBox plugin that extends the intent model, a single controller that plans and applies, and the controller's own Postgres. Only the controller talks to devices and to the PVE API.

```
            sharpstack ──writes intent──▶ NetBox (+ sharpwire plugin)
                 │                              │ GraphQL + event rules
                 └─plan requests / callbacks (later)─▶ controller ──▶ Postgres (plans, runs, audit)
                                                │
              ┌──────────────┬──────────────────┼─────────────────┬──────────────────┐
         RouterOS REST   RouterOS REST       PVE API          SSH (helper)      VyOS HTTP API
          cores (CCR)     leafs (CRS)      SDN, cluster files  node files       gateways (phase 2)
                                                │
                       alerts in Postgres + web UI (Alertmanager, chat later)
```

sharpstack never talks to network devices or to the PVE SDN API. In v1 it writes only its own NetBox objects (tenants, VMs, IPs). An admin creates public network capacity in NetBox, which triggers sharpwire's plans. Later, it also calls the sharpwire API to request plans and receive callbacks. In v1, alerts are stored in the controller's Postgres and shown in the web UI; Alertmanager and chat come later. The stack is Python throughout: a Django plugin, FastAPI, and a Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`), with no Redis or Celery. The rest follows sharpstack's baseline (see [sharpstack integration](09-sharpstack-integration.md#reused-from-sharpstack)).

## Targets

A **target** is anything sharpwire plans against with its own lock and live state:

- one device per RouterOS box or VyOS VM;
- one target per Proxmox node, covering that node's files;
- one target for the Proxmox cluster, covering SDN and the cluster files in `/etc/pve`.

The cluster target exists because SDN and `/etc/pve` are cluster-wide. One API call or file write changes every node at once, so it can't be rolled out node by node (see [Apply and rollout](05-apply-and-rollout.md)).
