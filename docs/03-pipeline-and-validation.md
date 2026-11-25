# 3. Pipeline and validation

Nov 20, 2026 · Draft

Push and drift detection share one pipeline: a non-empty plan means the target differs from NetBox, whatever the cause.

## Pipeline

1. **Build intent.** Read NetBox into the intent model ([Intent model](02-intent-model.md)).
2. **Validate.** Per-target checks, cross-target checks, then the hard guards.
3. **Render.** The driver turns intent into its own objects:
   - RouterOS: resources keyed by natural keys, never `.id`.
   - Proxmox SDN: API objects keyed by ID.
   - Proxmox node and cluster files: whole files, parsed into stanzas so the diff stays readable.
4. **Fetch live state.** Read via RouterOS REST, the PVE API, or SSH (node files). Normalize, then drop dynamic entries and the driver's baseline.
5. **Diff.** Object-level add, set and remove, plus move ops for ordered lists. Ops are sorted by dependency and make-before-break: adds and sets first, referenced objects before the objects that use them, then removes, dependents first. The inverse ops come from simulating the plan on the live state and diffing back to it, and the same simulation proves the plan converges: diffing its result against the render gives no ops.
6. **Plan.** Immutable and hashed. It holds the NetBox changelog position it was built from, a live-state fingerprint, ordered ops, inverse ops, a readable diff, and any [plan warnings](02-intent-model.md#plan-warnings).

Each driver pins the RouterOS or PVE versions it supports and refuses to plan against anything else.

## Plan lifecycle

- **One open plan per target.** A new plan supersedes the target's previous unapproved one. The same plan seen again (same hash: target, ops and live-state fingerprint) is kept, so drift seen every 5 minutes is one plan. An empty plan supersedes the open one with nothing: the target is back in sync.
- **One kind of plan.** sharpwire doesn't classify plans as intent or drift. The plan shows the NetBox changelog entries since the target's last successful apply, so the approver can see whether NetBox changed or the device did.
- **Rejection.** A rejected plan is recorded by its hash, and the same hash isn't raised again.
- **Stale.** If live state changed before apply, the approval is void and the recomputed plan needs a fresh one ([Stale check](05-apply-and-rollout.md#stale-check)).
- **sharpwire's own NetBox writes** (VNet `status`) use a dedicated NetBox user that event rules ignore.

## Validation

### Per target

- Schema, and every address inside its VLAN's prefix.
- No duplicate addresses, BGP peers, VNIs or VLAN IDs.
- MTU: zone MTU + 70 ≤ underlay MTU; every port's `l2mtu` is at least its bond's MTU + 4.
- Proxmox: no gateway on underlay VLANs, `accept_ra 0` on static interfaces, and `source /etc/network/interfaces.d/*` present.

### Cross-target

These checks are the main reason to drive the whole network from one model. Each one guards a failure the network doc calls out, often one that BFD or BGP can't detect. Most are implemented in `app/intent/models.py`.

| Rule | Guards against |
| --- | --- |
| Node ASNs are unique and differ from the core, handoff and overlay ASNs; router-ids are unique | A node's /128 dropped as an AS-path loop; BGP confusion |
| Each underlay VLAN has exactly one core; each core is on exactly one underlay VLAN; every node has an address on every underlay VLAN | Half the ECMP paths missing |
| Node loopbacks are /128s inside the VTEP range | Loopbacks the cores' filters reject |
| Each core has exactly one `ul-in-*` chain and connection per managed node, and none for absent nodes | A missing node /128, or stale sessions |
| The SDN controller's peers are exactly the managed nodes' loopbacks | Missing EVPN sessions |
| Leaf bonds carry the right VLANs: node bonds carry mgmt, every underlay VLAN and the handoff; a core's bond carries mgmt, that core's underlay VLAN and the handoff; the peer-link carries every VLAN any bond carries | Broken node pairs that BFD can't see (network doc Step 4c) |
| The same bonds, `mlag-id`s, peers and VLANs on both leafs; the peer-link is not an MLAG bond | MLAG not forming one LAG |
| Public subnets fall inside a public range and within its maximum length | Subnets not advertised, or /32s leaked |

### Hard guards

A plan is rejected, with no way to override, if it:

- changes what the controller's management session depends on. On a node: the mgmt address, gateway and VLAN on `bond0.10`, the members of `bond0`, and the IPv6 default route. On a RouterOS device: its mgmt VLAN interface (other than its MTU, which is a [warning](02-intent-model.md#plan-warnings)) and its addresses, and removing the interface it sits on, now or once applied. Adopting one of these rows, by setting only sharpwire's marker, is allowed;
- changes more than one corosync link (once corosync apply exists);
- would leave a redundancy group (the core pair, the leaf pair, the PVE cluster) with no healthy member, for example by touching both cores in one step;
- removes a VNet, subnet or zone that is still in use: NetBox holds IP addresses inside its prefixes, or a sharpstack `PlatformSubnet` references it. Without this, deleting a VNet in NetBox would remove the bridge under running VMs.
