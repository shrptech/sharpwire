# Later and phase 2

Nov 20, 2026 · Draft

Everything here is designed but **not built in v1**. The v1 docs (01–09) describe only what v1 does. When an item moves into scope, move its text into the v1 doc it belongs to and delete it here.

## Later in phase 1

### Change sets

A NetBox change that touches several targets becomes one **change set**: one approval for the whole change, and a staged rollout in the order [Apply and rollout](05-apply-and-rollout.md#v1-procedures) lists for the operator in v1. Drift plans are never grouped. Health checks then also verify, at the end of the change set, the sessions that were "new" on earlier stages. In v1 they're only reported. Adds a `change_set_id` to plans by migration.

### Change classes and the sharpstack API

A change class is a declarative policy: the target types it applies to, the object types and fields that may change, add-only operations, value constraints and a max op count. A change set auto-applies only if all its ops match one class. It still goes through the stale check, armed rollback and health checks. Removals always go to a human, drift never matches a class, and no class touches a core, a leaf or a node's host files. Adds a `change_class` to plans by migration.

- **`public-vnet`:** add a VNet in zone `public` and its subnets inside the public ranges. No router config changes, because the cores and nodes already accept those ranges.
- **`customer-onboard`** (phase 2): see below.

The sharpstack API lets sharpstack request plans by scope and receive status callbacks. A controller-issued token can request plans and read status, but can't approve. sharpstack must show `awaiting_approval` and `blocked_by_freeze` to customers instead of timing out.

### Plugin allocation

A plugin API call allocates numbers in one NetBox transaction, so callers never pick them and two requests can't race: public VNet tags and subnets now, and customer NN, NAT IPv4 and /56 in phase 2.

### Freezes

A freeze blocks all applies, change classes included, with no override. Drift detection keeps running and plans queue up; after the freeze they pass the stale check before applying. An incident fix during a freeze goes around sharpwire and shows up as drift.

### Overrides and roles

Changes to protected objects become blocked unless an admin overrides them, recorded in the audit log. In v1 they're only warnings on the plan ([Intent model](02-intent-model.md#plan-warnings)). Roles grow to viewer, approver and admin.

### More apply targets

- **Leafs:** apply, leaf by leaf, after a spike on RouterOS REST with MLAG.
- **Cluster files:**
  - `datacenter.cfg` is written through pmxcfs with no armed rollback.
  - `corosync.conf` changes one link at a time, never while the other link is down. The rollback is armed on every node: each helper restores its local `/etc/corosync/corosync.conf` and restarts corosync, which works even without quorum. This needs a spike.
- **Cluster firewall rules:** default policies, sharpwire's cluster rules and security groups, and node host rules (allowing TCP 179, UDP 3784-3785, 4784 and 4789 on the underlay VLANs). Writing needs Sys.Modify on `/`.
- **Guest isolation cross-check:** alert when a NIC on a public VNet lacks `firewall=1`, or its VM's firewall lacks `enable=1`/`ipfilter=1`. Needs `VM.Audit` on `/vms`. Today this is sharpstack's area.

### Core intent beyond v1

Upstream ISP sessions, their filters and announced aggregates (a plugin model, sessions protected), VRRPv3 for the mgmt gateway (NetBox FHRP groups), the firewall, and system services (config contexts). Once these are in NetBox, the cores' managed scope becomes fully declarative: any row in a managed menu that isn't rendered is removed.

### Alerting and credentials

- **Alertmanager:** critical alerts page, warnings don't. "Controller down" comes from Prometheus scraping the controller.
- **Chat:** the informational stream.
- **Prometheus metrics:** drift per target, runs by outcome, time since the last reconcile, queue depth.
- **Credentials:** stored in Postgres with envelope encryption (a data key per credential, wrapped by an external master key), with a set and rotate API that never returns them.

## Phase 2: VPC

### Network

One zone, VRF and L3VNI per customer (`cNN`, L3VNI `100NN`). Private IPv4 may overlap between customers; each customer gets one NAT IPv4 and a routed IPv6 /56. Traffic exits via a VyOS pair (network doc Step 6 and Appendix B). The cores get VLAN 310, the `nat-in` chain and NAT outside connections to each gateway.

### Derived values

| Value | Rule |
| --- | --- |
| Zone, VRF, L3VNI, VNet ID, L2VNI | `cNN`, `vrf_cNN`, `100NN`, `cNNnXX`, `1NNXX` |
| VyOS VRF table, VXLAN and bridge, veth names | `10NN`, `100NN`, `veth10NN`/`veth10NNo` |
| Transfer nets, NAT rule ID | `100.64.0.(2·NN−2)/31`, `fd00:64:NN::/127`, `10NN` |
| Route targets | auto `65000:<L3VNI>` |

### VyOS driver

- **Transport:** the HTTP API with commit-confirm. Fall back to the RouterOS pattern if commit-confirm is missing; spike first.
- **Scope:** the whole config tree, apart from the sharpwire login and dynamic entries.
- **Models:** the shared config (underlay, EVPN to every node and the other gateway, `outside` VRF, active/standby policy), per-VRF conntrack zones with optional `conntrack-sync`, and the per-customer block from network doc B.5.
- **Version gate:** the driver refuses to plan a second customer with an overlapping range unless the version is verified to separate conntrack per VRF.
- **Health checks:**
  - underlay sessions to both cores;
  - EVPN sessions to every node and the other gateway;
  - each customer VRF holding its type-5 subnets;
  - outside sessions to both cores;
  - conntrack-sync, if enabled;
  - an isolation check: `vrf_cNN` holds only its own prefixes and a default.
- **Rollout:** the standby first, then the active.

### `customer-onboard`

sharpstack writes the customer to NetBox, via allocation. The class adds:
- on both gateways, the B.5 block for NN;
- on the cluster, zone `cNN`, its VNets and subnets, and the SDN apply.

Every name and number must equal the derived value. The NAT IPv4 and the /56 must lie inside the existing aggregates. The order is: gateways (standby, then active), then cluster SDN, then the isolation and default-route checks. Offboarding runs in reverse and needs a human.

### Validation added in phase 2

- Inter-zone filters on the cores are stateless.
- Each customer's L3VNI, VRF and RT match between Proxmox and both gateways.
- The gateways never import 20000.
- Private ranges don't overlap within one customer.

### sharpstack's VPC model

sharpstack today has:
- one shared zone with no VRF;
- several VPCs per tenant;
- a /48 per VPC;
- one IPv4 pool with no overlap;
- no limit on customers.

The network doc has one zone per customer, a /56, overlapping private IPv4 with one NAT IPv4 each, and at most 99 customers. sharpstack's VPC model will be rewritten to match; the mapping is decided before phase 2 starts.

### Phase 2 risks and open questions

- **Risk:** a VNI or RT mismatch blackholes a customer. Both sides are rendered from one model and checked on live state.
- **Risk:** overlapping customer ranges collide in conntrack. Mitigated by the version gate.
- **Open:** drain the active gateway before disruptive changes? Draining resets IPv4 NAT sessions unless `conntrack-sync` is on.
- **Open:** DNAT and port forwards in `customer-onboard`?
- **Open:** may public and VPC VMs reach each other through the cores?
- **Open:** how do sharpstack VPCs map to customer zones?
