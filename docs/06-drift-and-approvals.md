# 6. Drift and approvals

Nov 20, 2026 · Draft

## Drift

Any non-empty plan means a target differs from NetBox. sharpwire never fixes that on its own: it raises an alert, and a person either approves the plan or updates NetBox to match the device.

Plans are built two ways:
- **every 5 minutes**, for every managed target;
- **on NetBox changes**, through event rules calling sharpwire's webhook (`POST /hooks/netbox`, signed with a shared secret). A change queues one reconcile of the whole fabric 10 seconds out, which the changes that follow join: every plan reads the whole intent anyway. The rules ignore sharpwire's own NetBox user.

Both are the same job (`app/jobs/reconcile.py`), run by the worker (`app/jobs/worker.py`), which is also the scheduler. It builds the intent once, syncs the targets, then reads and plans each managed target in turn. A target that can't be read gets an alert and doesn't stop the others. A managed target nothing has planned for three intervals is overdue.

Proxmox-specific sources of drift:
- SDN changes made in the GUI, including **pending** ones, which also block applies;
- edits made through the GUI's node network panel, which rewrites `/etc/network/interfaces` or leaves an `interfaces.new`;
- hand edits to `frr.conf.local`, `/etc/hosts` or `datacenter.cfg`;
- a merged FRR config that fails the post-merge check after an SDN apply.

## Approvals

A person approves every plan in the minimal web UI: plan list, diff, [warnings](02-intent-model.md#plan-warnings), then approve or reject, and apply. Anyone in the Kanidm group `sharpwire` can do all of it ([Security](07-security-and-observability.md#users)).

## sharpstack

In v1, sharpstack takes part only through NetBox ([sharpstack integration](09-sharpstack-integration.md)):
- an admin creates public capacity in NetBox;
- a person approves the plan;
- sharpwire sets the VNet `active`;
- sharpstack uses only `active` VNets.

The sharpstack API, change classes and freezes come [later](later.md).
