# 3. Pipeline and validation

Nov 20, 2026 · Draft

Push and drift detection share one pipeline. Drift is simply a non-empty plan with no NetBox change behind it.

## Pipeline

1. **Build intent.** Fetch from NetBox, compute derived values ([Intent model](02-intent-model.md#derived-not-stored)), and normalize into the vendor-neutral model.
2. **Validate.** Per-target checks, then cross-target consistency checks (below), then the hard guards.
3. **Render.** The driver maps intent to its own object model:
   - RouterOS: resources keyed by natural keys, never `.id`, which isn't stable.
   - VyOS: a config tree.
   - Proxmox SDN: API objects keyed by ID.
   - Proxmox node and cluster files: whole files, parsed into stanzas so the diff stays readable.
4. **Fetch live state.** Read via RouterOS REST, the PVE API, SSH (node files) or the VyOS HTTP API. Normalize, then drop dynamic entries and the driver's non-deletable baseline.
5. **Diff.** Object-level add, set and remove. Ordered lists such as firewall rules and filter chains produce move ops. Ops are sorted by dependency: referenced objects are created first, and references are removed before the objects they point to.
6. **Plan.** Immutable and hashed. It holds the NetBox changelog position, a live-state fingerprint per target, ordered ops, inverse ops, a human-readable diff, its kind (intent or drift) and its change class, if any.

Each driver declares the RouterOS, PVE or VyOS versions it supports and refuses to plan against anything else.

## Plan lifecycle

- **One open plan per target.** A new plan for a target supersedes that target's previous unapproved plan.
- **Intent or drift.** Each target records the NetBox changelog position of its last successful apply. If NetBox has changes to that target's intent after that position, the plan is **intent**; otherwise it is **drift**. A plan that couldn't be applied yet (for example, a node plan waiting for SDN) therefore stays intent, however many reconciles run.
- **Rejection.** A rejected plan is recorded by its hash. Reconciles that produce the same hash show it as rejected rather than raising it again; a different plan is raised normally.
- **Stale.** A stale plan's approval is void. The recomputed plan needs a fresh approval ([Stale check](05-apply-and-rollout.md#stale-check)).
- **sharpwire's own NetBox writes** (VNet `status`) are made by a dedicated NetBox user. Event rules ignore that user, and its changes don't count as intent changes.

## Change sets (later)

**v1:** one plan per target, each approved and applied separately. A NetBox change that touches several targets produces several plans. The operator applies them in the order given in [Apply and rollout](05-apply-and-rollout.md#change-set-stages), and each plan's stale check catches anything that changed in between. Plans already carry a `change_set_id`, which is unset in v1.

A NetBox change often touches several targets that must change together. For example, adding a node touches both leafs, both cores, the new node and the cluster's SDN controller. The controller groups the plans for one NetBox change into a **change set**:

- Every plan in it carries the change set ID.
- Approval is per change set, so a human approves the whole change or none of it.
- Rollout order across targets comes from the change set's stage list (see [Rollout](05-apply-and-rollout.md#rollout)).
- Drift plans are never grouped. They stay one plan per target.

## Validation

### Per target

- Schema, and IPs inside their prefixes.
- No duplicate BGP peers, VNIs or VLAN IDs.
- MTU: the bond and any VLAN carrying VXLAN ≥ 9216; zone MTU + 70 ≤ underlay MTU; leaf `l2mtu` ≥ 9220 and ≤ the port's `max-l2mtu`.
- Proxmox: no VLAN 101 or 102 interface carries a gateway; `accept_ra 0` on every static interface; `source /etc/network/interfaces.d/*` present.

### Cross-target consistency

These checks are the main reason to manage the whole network from one model. Each guards a failure the network doc calls out, often one that BFD or BGP can't detect.

| Rule | Guards against |
| --- | --- |
| Node underlay ASNs are unique and differ from the core ASNs and from 4200000100 | A remote node's /128 dropped as an AS-path loop (network doc, Troubleshooting) |
| Each core has exactly one underlay connection and `ul-in-*` chain per node (and per gateway in phase 2) on its VLAN, and nothing for absent targets | A missing node /128, or stale sessions left behind |
| Each node's `frr.conf.local` peers with exactly core A's VLAN 101 address and core B's VLAN 102 address | Half the ECMP paths missing |
| The EVPN controller peer list matches the set of VTEP loopbacks in NetBox (and, in phase 2, each gateway's VTEP neighbor list) | EVPN sessions missing for a node |
| Leaf VLAN membership matches the network doc's table: 10 on everything; 101 on nodes, core A and the peer-link; 102 on nodes, core B and the peer-link; 300 on nodes, both cores and the peer-link | Broken node pairs that BFD can't see (Step 4c) |
| The same `mlag-id` for a bond on both leafs; the peer-link in no MLAG bond | MLAG not forming one LAG |
| Public subnet sizes fall within both the nodes' `PUB-OUT` lengths and the cores' `pub-in` lengths | Subnets silently not advertised, or /32s leaked |
| `vrf_public` L3VNI 20000 appears only in zone `public` | Public routes in the wrong VRF |
| Inter-zone filters on the cores are stateless **(phase 2)** | One-way traffic from asymmetric paths across the two cores |
| Each customer's L3VNI, VRF and route target match between the Proxmox zone and both gateways; the gateways never import 20000 **(phase 2)** | Tenant traffic blackholed by an RT or VNI mismatch |
| Private ranges don't overlap within a customer; overlap between customers is allowed **(phase 2)** | Ambiguous routes inside one VRF |

### Hard guards

A plan is rejected, and no override can unblock it, if:

- it changes what the controller's own management session to the target depends on: on a node, the mgmt address, gateway and VLAN on `bond0.10`, the members of `bond0`, and the IPv6 default route; on a RouterOS box, its mgmt VLAN interface and address. Other attributes of these interfaces (MTU, `egress-qos-map`, LACP options) are protected objects: they can change, but only with an admin override;
- it touches a protected object without an admin override;
- it changes more than one corosync link in one plan;
- it would leave a redundancy group with no healthy member (for example, both leafs or both cores in one step);
- it removes a VNet, a subnet or a zone that is still in use: NetBox holds IP addresses inside its prefixes, a sharpstack `PlatformSubnet` references it, or PVE reports a guest NIC on its bridge. SDN is declarative, so without this rule, deleting a VNet in NetBox would remove the bridge under running VMs.
