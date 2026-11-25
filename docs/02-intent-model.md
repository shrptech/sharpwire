# 2. Intent model

Nov 20, 2026 · Draft

NetBox is the single source of truth. Every value that ends up on a device comes from one of two places:

- **a NetBox object** (mostly core models: devices, interfaces, VLANs, prefixes, IPs, ASNs, plus a few custom fields and two plugin models); or
- **a named rule** in this doc: a fixed shape or constant in code, such as a chain name or the LACP mode.

There is no third place. If a value isn't in the mapping or the rules below, it doesn't belong on a device. Hand-configured parts of v1 (leafs, corosync link1, bootstraps) still follow the same rules, so the read-only drivers can diff them.

The intent builder reads NetBox via GraphQL into the vendor-neutral model in `app/intent/models.py`. `app/services/netbox.py` reads a snapshot, one list per kind of object, page by page. `app/intent/builder.py` turns it into the model without any other input. The lists are separate requests, so an edit made while they're read can land between them; a plan's stale check catches that.

- **The fabric is what's tagged `sharpwire-managed`** ([sharpstack integration](09-sharpstack-integration.md#ownership)): devices, VLANs, prefixes and ASNs. Of devices, VLANs and prefixes, only the active ones count. A VLAN is matched by its NetBox object, not its VID, so an interface on another site's VLAN 102 is reported, not mistaken for the underlay.
- **Addresses:** only active IP addresses count, and an interface has at most one per family. A second active address is reported, never silently picked.
- **Bond peers** follow the members' cable paths (`connected_endpoints`), through patch panels.
- **Every active fabric device is in the model,** managed or not, so the routers peer with hand-run devices too. The `sharpwire_managed` custom field is read into the model as `managed`: what sharpwire may change.
- **Problems:** when NetBox is missing something or holds it in the wrong shape, the builder reports every problem at once, each saying what to fix in NetBox, then the model's cross-target validation adds its own, one sentence each.
- **Order:** it sorts what it builds (by natural name, VLAN ID, and addresses IPv6 first), so the same NetBox data always renders the same config.
- **Platform:** the driver is chosen by the device's NetBox platform (`routeros`, `proxmox`), and the builder rejects a device whose role and platform don't match.

The NetBox token needs view permission on devices, interfaces, device roles, platforms, cables, VLANs, prefixes, IP addresses, ASNs, IPAM roles and tags. NetBox hides what a token can't view instead of failing, so a missing permission shows up as missing objects.

Not read yet: the SDN zone and VNets, the nodes' cluster membership and `firewall_enabled`. They come with the drivers that use them (build plan step 3).

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
| SDN zone | Plugin model **Zone**: kind `public` (one zone of that kind; its PVE zone ID is the kind), VRF (`ipam.VRF`, one zone per VRF), L3VNI, MTU |
| VNet | Plugin model **VNet**: zone, VNI (also its tag and name `vnetNNN`, so at most 9999 to fit PVE's 8-character VNet ID), subnets (`ipam.Prefix` in the zone's VRF, each in at most one VNet, and protected from deletion or a VRF change while it is one; gateway as an anycast `ipam.IPAddress`), and `status` (`planned`/`active`, written by sharpwire through the REST API; the UI forms don't edit it, and changing an active VNet's VNI, zone or subnets puts it back to `planned`, so sharpstack stops using it until sharpwire re-applies it) |
| Cluster firewall | Custom field `firewall_enabled` on the NetBox cluster (v1 checks only this) |

sharpstack also writes to NetBox ([sharpstack integration](09-sharpstack-integration.md#ownership)). Event rules fire only on objects sharpwire uses, and never on sharpwire's own writes.

The two plugin models, the custom fields and the roles, platforms and tags above are all the plugin adds in v1. Everything else is NetBox core. The plugin creates the custom fields, roles, platforms and tags after every `migrate` (`netbox-plugin/sharpwire_netbox/provisioning.py`). It only adds what's missing, and logs anything it can't create as declared instead of overwriting an admin's change.

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
| Ownership marker | `comment=sharpwire` on every row sharpwire adds, other than filter rules and settings menus |
| Filter rule comments | `sharpwire:<chain>:<rule name>`, which also marks the rule as sharpwire's |
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
- a core's BGP instance (ASN, router-id), or a core's or leaf's mgmt VLAN interface MTU (anything else on it is a [hard guard](03-pipeline-and-validation.md#hard-guards));
- a node's `bond0`/`bond0.10` attributes outside the hard guard (MTU, bond options, `egress-qos-map`);
- a node's `dummy0`, ASN or router-id;
- any corosync setting.
