# 6. Drift and approvals

Nov 20, 2026 · Draft

## Drift

Any non-empty plan means a target differs from NetBox. sharpwire never fixes that on its own: it raises an alert, and a person either approves the plan or updates NetBox to match the device.

Plans are built two ways:
- **every 5 minutes**, for every managed target;
- **on NetBox changes**, through event rules, debounced per target. The rules ignore sharpwire's own NetBox user.

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
