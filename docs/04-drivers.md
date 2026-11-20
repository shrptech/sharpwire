# 4. Drivers

Nov 20, 2026 · Draft

Each driver defines its **managed scope**: what it reads, diffs and may change. Outside it, nothing is read for the diff and nothing is touched.

## RouterOS (cores and leafs)

**Transport.** RouterOS REST over HTTPS on the mgmt VLAN. Pinned to RouterOS 7.25.

**Managed scope in v1: the rows sharpwire renders.** In each RouterOS menu sharpwire renders into, it owns the rows its render produces. Other rows in those menus are reported in the plan as **unmanaged** and never removed. That leaves room for what v1 configures by hand on the cores: upstream ISP sessions (in the same `/routing/bgp/connection` menu), VRRP, the firewall and system services. Once those are in NetBox, the scope becomes fully declarative ([later](later.md#core-intent-beyond-v1)).

**Leafs are read-only in v1.** The driver renders and diffs them for drift and validation, but refuses to apply.

**What is rendered** comes from the intent and the RouterOS rules in [Intent model](02-intent-model.md#routeros). Cores:
- ports, the bond to the leafs, and VLAN interfaces with their addresses;
- the BGP instance, BFD, the filter chains, the per-node underlay connections, and the `pub4`/`pub6` listening connections.

Leafs:
- ports, the peer-link and the MLAG bonds;
- the bridge, MLAG, bridge ports and bridge VLANs;
- the mgmt interface and its address.

**Object identity.** Rows are keyed by natural keys, never by `.id`. The driver looks up the current `.id` for a natural key on every request; a failed `PATCH` by name only says "missing or invalid resource identifier".

| Row | Natural key |
| --- | --- |
| Interfaces, bonds, VLAN interfaces, BGP instance and connections | `name` |
| Bridge VLANs | `bridge` + `vlan-ids` |
| Bridge ports | `bridge` + `interface` |
| Addresses | `address` + `interface` |
| BFD configuration | `interfaces` |
| Routing filter rules | `chain` + `sharpwire:<chain>:<name>` comment, kept unique by the driver. Order is separate from identity: read from `.nextid`, changed with `place-before`/`move` |

Dynamic sessions from a listening connection all get the same name (`pub6-1`), so health checks key them by `remote.address`.

**Baseline (never removed).** Physical ports, the built-in groups (`read`, `write`, `full`), the sharpwire service account, and dynamic entries.

**REST behaviour the diff relies on** (RouterOS 7.25.1, `spikes/routeros/FINDINGS.md`):
- Values are strings, and booleans read back normalised (`yes` becomes `true`).
- **A field at its default value is left out of a GET**, so the driver keeps a table of defaults for every field it renders.
- `POST /rest/execute` returns HTTP 200 even when the command fails, so the driver checks `ret` every time.
- Deleting an interface leaves its addresses behind, which is why dependents are removed first.
- With `use-bfd=yes`, BGP stays down until BFD is up. Validation rejects a connection on an interface no BFD configuration row covers.

**Service account.** `read`, `write`, `api`, `rest-api` ([Security](07-security-and-observability.md#target-accounts)).

## Proxmox

Proxmox is three kinds of target: the cluster's SDN, the cluster's files and firewall, and each node's files.

### Cluster target: SDN

**Transport.** The PVE API, authenticated with a token. Pinned to PVE 9.x with libpve-network-perl 1.6.

**Managed scope.** Everything under SDN: the controller (`evpnctl`), zones, VNets and subnets. sharpwire is the only SDN writer, so any SDN object not in NetBox is removed.

**Live state** is the running SDN config. A plan is only valid while the **pending** config is empty or exactly the plan's own staged ops, so changes someone staged in the GUI can never ride along.

**Apply** (`spikes/pve-sdn/FINDINGS.md`):
1. `POST /cluster/sdn/lock` returns a token. It refuses while anything is pending.
2. Stage the ops with the token. Compare `?pending=1` with the plan, and preview each node's FRR change with `GET /cluster/sdn/dry-run?node=<n>`.
3. `PUT /cluster/sdn` with the token and `release-lock=0`. PVE commits the new config **first**, then reloads the nodes **one at a time, in no fixed order, and carries on past failures**. The apply task reports success even if a node failed, so the controller reads each node's `srvreload` task and `/cluster/resources?type=sdn`.
4. Release the lock.

On each node, the reload:
- promotes `interfaces.new` and writes `interfaces.d/sdn`;
- runs `ifreload -a`; if that fails, FRR isn't regenerated on that node;
- regenerates `frr.conf`, merging `frr.conf.local`;
- reloads FRR with `frr-reload.py`, falling back to `systemctl restart frr` if the reload fails.

Things the controller works around:
- **The lock has no expiry**, so crash recovery force-releases it.
- **Subnets write to PVE IPAM immediately**, and `/rollback` doesn't undo that.
- **At boot, `pve-sdn-commit.service` commits anything pending**, ignoring the lock. Staged changes never outlive one apply.

### Cluster target: files and firewall (read-only in v1)

- **`/etc/pve/corosync.conf`:** the nodelist and both links with their priorities ([rules](02-intent-model.md#proxmox)). Corosync link1 is added by hand once, following network doc Step 3.
- **`/etc/pve/datacenter.cfg`:** only the `migration` key.
- **Cluster firewall:** only the `enable` option, read from `/cluster/firewall/options`. Modelling rules comes [later](later.md#more-apply-targets). Per-VM firewall config and ipsets belong to sharpstack.

All three are read through the PVE API or the node helper, and only diffed. Applying them comes [later](later.md#more-apply-targets).

### Node targets: host files

**Transport.** SSH as a dedicated `sharpwire` user, with sudo restricted to one helper, `sharpwire-node`. The helper reads and writes the managed files, validates, arms and disarms the rollback, and reloads. A versioned bootstrap package installs the user, the helper, the sudoers rule and `sharpwire-restore.service`. An operator runs it once per node, and again to upgrade the helper. The controller refuses to plan for a node whose helper is older than it supports.

**Managed scope.** Whole files, diffed by stanza:

| File | Contents |
| --- | --- |
| `/etc/network/interfaces` | `lo`, the NICs, `bond0`, `bond0.10`, the underlay VLAN interfaces, `dummy0` and `bond0.300` in `vrf_public` (network doc Steps 2, 3, 5c). `interfaces.d/sdn` belongs to SDN |
| `/etc/sysctl.d/90-sdn.conf` | the sysctl rule |
| `/etc/frr/frr.conf.local` | `UL-IN`/`UL-OUT`, the underlay in `router bgp 65000`, and the `vrf_public` handoff (network doc Steps 4a, 5c) |
| `/etc/hosts` | every node's mgmt address |

**Rules for `frr.conf.local`.** PVE's merge isn't a plain append (`spikes/pve-sdn/`, `spikes/node-linux/`):
- No top-level `bfd`, `ip route` or `ip nht` blocks; the merge drops them. BFD uses FRR's default timers.
- Never repeat a setting SDN generates. A duplicate `bgp router-id` or `advertise` line swaps with the generated one on every reload and resets every BGP session. The router-id comes from `dummy0`'s MAC, and zone `public` uses `advertise-subnets 0`.
- Write `no neighbor CORE6 activate`. FRR rejects `neighbor CORE6 deactivate`.

**Validate** with `ifreload -a -s` (`-n` is only a preview and exits 0 even on a broken file) and `frr-reload.py --test`. The helper creates `/run/network` first, because ifupdown2 fails with a misleading "another instance is running" error without it.

**Apply.** Write the files, run `sysctl --system`, then `pvesh set /nodes/<self>/network --regenerate-frr 1`. That runs the same per-node steps as an SDN apply. pvesh exits 0 even when the task fails, so the helper checks the task's status itself.

**`vrf_public` must exist first.** `bond0.300` needs `vrf_public`, which only exists once SDN has been applied on that node. If it's missing, ifupdown2 exits 1 but still brings `bond0.300` up in the default VRF. So the helper checks `ip link show vrf_public type vrf` before applying, and validation rejects the plan.

**Post-merge check**, after any SDN apply or `frr.conf.local` change:
- the router-id in every BGP instance is the one encoded in `dummy0`'s MAC (`show bgp vrf all summary json`);
- `update-source dummy0` is on the `VTEP` peer group, and `NO-DEFAULT` is on both `vrf_public` advertise lines;
- every rendered line is present, with route-maps compared by content, never by sequence number (the merge renumbers them);
- **reload-stable:** a second `frr-reload.py --reload` changes nothing and resets no session.

**Drift** is a normalised comparison of the rendered and live state. `ifquery --check -a` misses a deleted default route, interfaces running outside the file, and removed stanzas, so the helper checks those itself.
