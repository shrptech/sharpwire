# 6. Drift, approvals and change classes

Nov 20, 2026 · Draft

## Drift detection

Drift is detected, alerted and queued for a human. It is never fixed automatically.

Plans are triggered two ways:

- **Scheduled reconciliation** runs the full pipeline for every managed target every 5 minutes, which is cheap at 10–50 targets.
- **NetBox event rules** trigger plans on change, debounced per target, so a bulk edit produces one plan per target (later: one change set). Event rules ignore sharpwire's own NetBox user.

A drift plan (the target changed, NetBox didn't):

- raises an alert and appears in the UI as pending approval;
- can be remediated by applying it, or acknowledged while NetBox is updated to match the target;
- never matches a change class, so it is never auto-applied.

Drift sources specific to Proxmox:

- SDN changes made in the GUI. Both applied changes and **pending** changes count, and pending changes also block applies (see [Drivers](04-drivers.md#cluster-target-sdn)).
- Edits through the GUI's node network panel, which rewrites `/etc/network/interfaces`, or a pending `interfaces.new`.
- Hand edits to `frr.conf.local`, `/etc/hosts`, or the managed keys in `datacenter.cfg`.
- The merged FRR config losing a required line after an SDN apply (post-merge check).

## Approvals

**v1:** every plan needs a human approver, using the minimal web UI (plan list, diff view, approve or reject, apply).

**(later)** Every change set needs human approval unless a change class covers every op in it.

## Change classes (later)

Not in v1. Plans already carry a `change_class` field, which is always unset in v1, so the classifier can be added without migrating data.

A change class is a declarative policy: the roles and target types it applies to, the object types and fields that may change, the allowed operations, value constraints, and a max op count. A change set auto-applies only if all its ops match one class. Auto-applied change sets still go through the stale check, armed rollback and health checks. The audit log records the class name and the sharpstack request ID as the approver.

These rules hold for every class:

- Classes allow add only, plus `set` on the objects the class itself created. Removals always go to a human, because a wrong delete is the expensive mistake.
- Drift plans never match a class.
- No class may touch a protected object, a core, a leaf, or a Proxmox node's host files.

### `public-vnet` (phase 1)

Lets sharpstack add shared public capacity: a new VNet in the public zone. Public VNets are shared platform capacity, not per customer.

| Allowed | Constraint |
| --- | --- |
| Add a VNet in zone `public` | Tag allocated by the plugin from 200–999, VNet named `vnetNNN` after its tag |
| Add subnets to that VNet | IPv4 inside `198.51.100.0/24` and IPv6 inside `2001:db8:70::/48`, both allocated by the plugin; prefix lengths within the nodes' `PUB-OUT` and the cores' `pub-in` lengths; the gateway is the first address |
| SDN apply | Only if the pending config equals exactly these ops |

Because the subnets fall inside ranges the cores and nodes already accept and advertise, no router config changes. That is what makes this class safe to automate. A subnet outside those ranges fails validation and needs a human to widen the ranges first.

### `customer-onboard` (phase 2)

Lets sharpstack bring up a VPC customer end to end. sharpstack only writes the customer to NetBox, using the plugin's allocation call.

| Target | Allowed adds |
| --- | --- |
| Both gateways | The network doc's B.5 block for this NN: VRF, VXLAN, bridge, veth pair, the two defaults in `cNN`, the /32 and /56 routes in `outside`, and the SNAT rule to the customer's NAT IPv4 |
| Cluster SDN | Zone `cNN`, VNets `cNNnXX` and their subnets, SDN apply |

Constraints:

- Every name and number must equal the value derived from the customer's NN ([Intent model](02-intent-model.md#derived-not-stored)).
- The NAT IPv4 must lie inside `203.0.113.0/24` and the /56 inside `2001:db8:60::/48`, so the cores' `nat-in` and the gateways' aggregates need no change.
- The class also covers adding a VNet or subnet to an existing customer zone.
- DNAT and port forwards are **not** in the class yet (see open questions).

## Freezes (later)

Not in v1. Every apply already passes through one gate in the job queue, which is where the freeze check plugs in.

A freeze blocks all applies, change classes included. Freezes are absolute: there is no override, not even for admins. Drift detection keeps running, and new plans are created and queued. When the freeze ends, queued plans pass the stale check before they apply.

An incident fix during a freeze therefore goes around sharpwire: by hand, through OOB if needed. It shows up as drift at the next reconcile. After the freeze, someone either updates NetBox to match the fix (acknowledging the drift) or approves the drift plan to revert it.

## sharpstack integration

sharpstack never talks to network devices or to the PVE SDN API.

**v1:** an admin creates public capacity in NetBox, and a human approves the plan. sharpstack uses a VNet only once sharpwire has set the VNet's `status` to `active`. sharpstack must first stop writing SDN itself (see the [prerequisites](09-sharpstack-integration.md#sharpstack-prerequisites-for-sharpwire-v1)).

**(later)** sharpstack also calls the sharpwire API to request a plan, and receives status through callbacks:

- **Auth.** A controller-issued API token, scoped to requesting plans and reading status. It cannot approve anything.
- **Requests.** sharpstack requests plans by scope: a public VNet, a customer (phase 2), or a list of targets. sharpwire builds the change set.
- **Outcomes.** Change sets that match a class apply automatically; all others wait for a human.
- **States.** sharpstack must handle `awaiting_approval` and `blocked_by_freeze` and show them to customers instead of timing out.
- **PVE permissions.** PVE users and ACLs, such as `SDN.Use` on `/sdn/zones/cNN`, stay with sharpstack. sharpwire doesn't manage identities.
