# 2. Intent model

Nov 20, 2026 · Draft

NetBox is the single source of truth. Every value that ends up on a device comes from one of two places:

- **a NetBox object** (mostly core models: devices, interfaces, VLANs, prefixes, IPs, ASNs, plus a few custom fields and two plugin models); or
- **a named rule** in this doc: a fixed shape or constant in code, such as a chain name or the LACP mode.

There is no third place. If a value isn't in the mapping or the rules below, it doesn't belong on a device. Hand-configured parts of v1 (leafs, corosync link1, bootstraps) still follow the same rules, so the read-only drivers can diff them.

The intent builder reads NetBox via GraphQL into the vendor-neutral model in `app/intent/models.py`. The driver is chosen by the device's NetBox platform (`routeros`, `proxmox`). Devices are managed when their `sharpwire_managed` custom field is set.

## NetBox mapping

| Intent | NetBox |
| --- | --- |
| Overlay ASN, handoff ASN | `ipam.ASN` tagged `overlay` / `public-handoff` |
| Mgmt VLAN | `ipam.VLAN` with role `mgmt` |
| Underlay VLANs and their prefixes | `ipam.VLAN` with role `underlay`, and the `ipam.Prefix` assigned to each |
| Public handoff VLAN and its prefixes | `ipam.VLAN` with role `public-handoff`, and its IPv4 and IPv6 prefixes |
| VTEP loopback range | `ipam.Prefix` with role `vtep` |
| Public ranges | `ipam.Prefix` with role `public`, plus custom field `max_subnet_length` (subnets from the prefix length up to this are accepted) |
| Device kind | Device role `core`, `leaf` or `pve-node`; nodes also belong to the NetBox virtualization cluster |
| Device ASN | Device custom field `asn` (an object field pointing at an `ipam.ASN`). NetBox assigns ASNs to sites, not devices, so the custom field carries the link |
| Router-id | Device custom field `router_id` |
| Physical port: name, MTU | `dcim.Interface` name and `mtu`, plus custom field `l2mtu` |
| Bond (LAG) | `dcim.Interface` of type LAG; members are the interfaces whose `lag` is set to it; `mtu`; `mode=tagged` with `tagged_vlans` |
| Bond peer | The device at the far end of the members' cables |
| MLAG id | Interface custom field `mlag_id`, on MLAG bonds only |
| Leaf bridge, mgmt interface | `dcim.Interface` of type bridge; a virtual interface with the bridge as parent and the mgmt VLAN as untagged VLAN |
| VLAN interface (core `vlan101`, node `bond0.101`) | Virtual `dcim.Interface` with its VLAN as untagged VLAN, `mtu`, and the IP addresses assigned to it |
| Addresses | `ipam.IPAddress` assigned to the interface, with its mask |
| Node loopback | The /128 `ipam.IPAddress` on the node's `dummy0` |
| SDN zone | Plugin model **Zone**: kind `public`, VRF (`ipam.VRF`), L3VNI, MTU |
| VNet | Plugin model **VNet**: zone, VNI (also its tag and name `vnetNNN`), subnets (`ipam.Prefix` in the zone's VRF, gateway as an anycast `ipam.IPAddress`), and `status` (`planned`/`active`, written by sharpwire) |
| Cluster firewall | Custom field `firewall_enabled` on the NetBox cluster (v1 checks only this) |

sharpstack also writes to NetBox ([sharpstack integration](09-sharpstack-integration.md#ownership)). Event rules fire only on objects sharpwire uses, and never on sharpwire's own writes.

The two plugin models, the custom fields and the roles and tags above are all the plugin adds in v1. Everything else is NetBox core.

## Rules

Fixed shapes and constants in code. Each has one home in code, named after the rule (for RouterOS, `app/drivers/routeros/render.py`). Changing a rule means changing this table and that code together.

### RouterOS

| Rule | Value |
| --- | --- |
| Bonds | `mode=802.3ad`, `lacp-rate=1sec`; core bonds also `transmit-hash-policy=layer-3-and-4` |
| IPv6 addresses | `advertise=no` |
| BGP instance | name `core`, with the device's ASN and router-id |
| Underlay, per node | chain `ul-in-<node>`: accept the node's loopback, reject. Connection `ul-<node>`: eBGP over IPv6 from the core's address on its underlay VLAN to the node's address on the same VLAN, `use-bfd=yes`, `nexthop-choice=propagate`, in `ul-in-<node>`, out `ul-out` |
| `ul-out` | accept /128s inside the VTEP range, reject |
| Public handoff | listening connections `pub4` and `pub6` with remote address set to the handoff prefixes and remote AS the handoff ASN, `use-bfd=yes`, `output.default-originate=always`, in `pub-in`, out `default-out` |
| `pub-in` | accept each public range from its length up to `max_subnet_length`, reject |
| `default-out` | accept `0.0.0.0/0` and `::/0`, reject |
| Filter rule comments | `sharpwire:<chain>:<rule name>` |
| BFD | 300 ms × 3 (FRR's defaults, used on the nodes); one `/routing/bfd/configuration` row covering the interfaces with BFD-enabled sessions |
| Leaf bridge | `vlan-filtering=yes`; MLAG peer port is the LAG cabled to the other leaf; bridge VLANs from the LAGs' tagged VLANs; the bridge itself is tagged where the leaf has a VLAN interface |
| Leaf edge bonds | `frame-types=admit-only-vlan-tagged` on MLAG bonds |
| Leaf STP and ICCP | constants from MikroTik's MLAG documentation for the pinned RouterOS version, written down when the leafs are first configured |

### Proxmox

| Rule | Value |
| --- | --- |
| Node bond | `bond-mode 802.3ad`, `bond-xmit-hash-policy layer3+4`, `bond-lacp-rate fast`, `bond-miimon 100` |
| Static interfaces | `accept_ra 0`; no gateway on underlay VLANs |
| Mgmt VLAN | `egress-qos-map 0:6` (corosync priority) |
| `dummy0` | `hwaddress 02:00:` followed by the router-id's four bytes, which is how PVE derives the router-id (network doc Step 2) |
| sysctl | the four lines in network doc Step 2 |
| `frr.conf.local` | network doc Steps 4a and 5c, filled from the node's ASN, loopback, underlay peers and the public ranges, following the merge rules in [Drivers](04-drivers.md#node-targets-host-files) |
| `/etc/hosts` | every node's mgmt address and DNS name |
| SDN controller | `evpnctl`, ASN = overlay ASN, peers = every managed node's loopback |
| Zone `public` | `advertise-subnets 0`, `ipam pve`, no exit nodes, no SNAT |
| Corosync | link0 on the mgmt address, link1 on the loopback; `knet_link_priority` 20 on link0 and 10 on link1 |
| `datacenter.cfg` | `migration: secure,network=<mgmt prefix>` |

## Allocation

In v1 an admin creates VNets and subnets in NetBox by hand. Validation rejects collisions and anything outside the public ranges. Plugin allocation comes [later](later.md#plugin-allocation).

## Plan warnings

Some changes are legal but risky, so the plan shows them as **warnings** that the approver must read before approving. There is no separate override step in v1, because every plan is already approved by a human ([later](later.md#overrides-and-roles)). Plans that would break the management path are a different matter: they're rejected outright by the [hard guards](03-pipeline-and-validation.md#hard-guards).

Warnings are raised for changes to:
- the leaf MLAG config, or the peer-link's VLANs;
- a core's mgmt VLAN interface, or its BGP instance (ASN, router-id);
- a node's `bond0`/`bond0.10` attributes outside the hard guard (MTU, bond options, `egress-qos-map`);
- a node's `dummy0`, ASN or router-id;
- any corosync setting.
