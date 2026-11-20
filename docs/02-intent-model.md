# 2. Intent model

Nov 20, 2026 · Draft

NetBox is the single source of truth, and config is strictly declarative: anything in a managed scope that isn't in NetBox is removed. Each driver defines its managed scope exactly (see [Drivers](04-drivers.md)). Anything outside it is never read for diffing and never touched.

## Where intent lives

| Config area | Where it lives |
| --- | --- |
| Devices, platforms, roles, interfaces, bonds, VLANs, IPs, prefixes, cabling | NetBox core |
| Proxmox nodes | NetBox core devices, assigned to a NetBox virtualization cluster |
| VRFs (`vrf_public`, phase 2: `cNN`, `outside`) | NetBox core VRF |
| EVPN: L2 VNIs (one per VNet) and L3VNIs (one per zone), route targets | NetBox core L2VPN (`vxlan-evpn`) and route-target models |
| ASNs (core, node underlay, public handoff, overlay 65000) | NetBox core ASN model, with ranges for allocation |
| SDN zones and VNets | sharpwire plugin: **Zone** (kind `public` or `customer`, VRF, L3VNI, MTU, IPAM) and **VNet** (zone, L2VNI, subnets as NetBox prefixes in the zone's VRF with a gateway IP, and a `status` of `planned` or `active` that sharpwire sets once the VNet is applied). No NetBox VLAN object is created for a VNet |
| Cluster-level PVE firewall: `enable` in v1; **(later)** cluster rules and host rules | sharpwire plugin |
| BGP sessions and peer groups, BFD profiles, including the upstream ISP sessions and the aggregates announced to them | sharpwire plugin |
| Route policy: prefix lists, route maps, RouterOS filter chains | sharpwire plugin |
| Firewall: address lists, ordered rule sets | sharpwire plugin |
| NAT rules **(phase 2)** | sharpwire plugin |
| Customers **(phase 2)**: NN, tenant, NAT IPv4, IPv6 /56, zone | sharpwire plugin **Customer**, linked to a NetBox tenant |
| Corosync links, cluster hosts, migration network | sharpwire plugin, on the NetBox cluster |
| System services: users and keys, NTP, DNS, syslog, SNMP | Plugin models, with defaults in config contexts by site and role |
| Redundancy groups (core pair, leaf pair, PVE cluster, phase 2: gateway pair) | sharpwire plugin |
| Managed flag, protected objects | Plugin custom fields |

sharpstack also writes to NetBox. [sharpstack integration](09-sharpstack-integration.md#ownership) lists which objects each system owns. Event rules fire only on sharpwire's objects.

The driver is selected from the NetBox platform field (`routeros`, `proxmox`, `vyos`). The intent builder pulls from NetBox via GraphQL into a vendor-neutral model (Pydantic). Every plan records the NetBox changelog position it was built from.

## Derived, not stored

Anything the network doc defines by a numbering rule is computed by the intent builder, never stored. A person can't set it to an inconsistent value, and the validator checks the inputs instead.

| Derived value | Rule | Source |
| --- | --- | --- |
| Node underlay ASN | from the node's ASN assignment, unique per node | NetBox ASN |
| BGP router-id | 10.255.0.<host part of the node's loopback> | network doc, Step 4 |
| RouterOS per-peer filter chain | `ul-in-<device>` | network doc, A.2 |
| `UL-OUT` and `network` lines in `frr.conf.local` | the node's own loopback /128 | network doc, Step 4 |
| EVPN controller peer list | every managed VTEP loopback (nodes, plus gateways in phase 2) | network doc, Step 5a |
| `/etc/hosts` and corosync `ring0_addr` | node mgmt addresses on VLAN 10 | network doc, Step 3 |
| Corosync `ring1_addr` and link priorities | the node's VTEP loopback; link0 high priority, link1 low | network doc, Step 3 |
| **(phase 2)** zone ID, VRF, L3VNI, VNet ID, L2VNI | `cNN`, `vrf_cNN`, `100NN`, `cNNnXX`, `1NNXX` | network doc, Step 5b |
| **(phase 2)** VyOS VRF table, VXLAN, bridge, veth names, transfer nets, NAT rule ID | `10NN`, `100NN`, `veth10NN`/`veth10NNo`, `100.64.0.(2·NN−2)/31`, `fd00:64:NN::/127`, `10NN` | network doc, B.5 |
| **(phase 2)** route targets | auto `65000:<L3VNI>` | network doc, Step 6 |

## Allocation

**v1:** an admin creates VNets and subnets in NetBox directly. sharpstack doesn't create them. Validation rejects collisions and anything outside the ranges below.

**(later)** The plugin owns allocation, so sharpstack never picks numbers itself and two requests can't race:

- **Phase 1.** Public VNet tags come from 200–999, and each VNet is named `vnetNNN` after its tag. That range doesn't collide with the VPC L2VNIs (`1NNXX`) or L3VNIs (`100NN`, 20000). Public subnets come from the public aggregates (`198.51.100.0/24` and `2001:db8:70::/48`).
- **Phase 2.** Customer NN (1–99), the NAT IPv4 from `203.0.113.0/24`, and the IPv6 /56 from `2001:db8:60::/48`.

Allocation is a plugin API call that creates the objects in one NetBox transaction.

## Protected objects

Protected objects can never change in an automated plan. Changing one needs an explicit override in the UI by an admin, recorded in the audit log. The mgmt-path values covered by the [hard guard](03-pipeline-and-validation.md#hard-guards) can't be changed at all, not even with an override.

| Area | Protected |
| --- | --- |
| Leafs | MLAG config, the peer-link bond and its membership in every VLAN, VLAN 10 |
| Cores | VLAN 10 interface, VRRP for the mgmt gateway, the BGP instance (ASN, router-id), upstream sessions |
| Proxmox nodes | Other attributes of `bond0` and `bond0.10` (MTU, LACP options, `egress-qos-map`), the `dummy0` address, the node's underlay ASN and router-id. The mgmt address, gateway, VLAN, bond members and default route fall under the hard guard instead |
| Proxmox cluster | Every corosync link, `config_version` handling, and the cluster name |
| Gateways **(phase 2)** | VLAN 101 and 102 interfaces, `dum0`, the underlay ASN |

The pipeline also has a hard guard that is separate from protected objects: it never removes or changes anything the controller's own management session to that target depends on.
