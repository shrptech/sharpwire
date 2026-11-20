# 4. Drivers

Nov 20, 2026 · Draft

Each driver defines its **managed scope**: what it reads, diffs and may change. Inside that scope, config is declarative and anything not in NetBox is removed. Outside it, nothing is read for the diff and nothing is touched. Each driver pins the versions it supports and refuses to plan against anything else.

## RouterOS (cores and leafs)

**Transport.** RouterOS REST over HTTPS on the mgmt VLAN.

**Object identity.** Resources are keyed by natural keys, never by `.id`:

| Resource | Natural key |
| --- | --- |
| Interfaces, bonds, VLAN interfaces | `name` |
| Bridge VLANs | `bridge` + `vlan-ids` |
| Bridge ports | `bridge` + `interface` |
| BGP instance / connection | `name` |
| Routing filter rules | `chain` + position (ordered list → move ops) |
| BFD configuration | `interfaces` set |
| Addresses | `address` + `interface` |
| Firewall rules | `chain` + position, with a rule comment carrying the sharpwire rule ID |

**Baseline (never removed).** Physical ethernet and SFP ports, the default `admin` group, the service account sharpwire uses, and dynamic entries.

**Core models (phase 1).** Ethernet `l2mtu` and `mtu`, the `bond-leafs` bond, VLAN interfaces 10, 101 or 102, and 300, addresses, the BGP instance, underlay connections (one per node, `nexthop-choice` set per the network doc's Step 4b), per-node `ul-in-*` chains, `ul-out`, the public handoff listening connections (`pub4`, `pub6`, with `remote.address` set to the VLAN 300 prefix and default-originate), `pub-in` and `default-out`, BFD, VRRPv3 on VLAN 10, firewall and system services. Upstream is managed in full: the ISP connections (protected), the inbound and outbound filters, and the announced aggregates (`198.51.100.0/24`, `2001:db8:70::/48`, plus the VPC aggregates in phase 2). Neither the handoff /24 nor private ranges are ever announced upstream.

**Core models (phase 2).** VLAN 310, the `nat-in` chain, and the NAT outside connections to each gateway.

**Leafs are read-only in v1.** The driver renders and diffs them for drift and validation, but refuses to apply. Leaf apply comes **(later)**.

**Leaf models.** Ethernet `l2mtu` and `mtu`, the peer-link bond, one MLAG bond per node and core, the bridge with `vlan-filtering`, bridge ports, bridge VLANs, MLAG, the mgmt VLAN interface and address, and system services.

**Rollback support.** RouterOS has no separate scheduler or script permission. A scheduled script runs with its owner's policies, so the service account needs `read`, `write`, `policy` and `test`, plus `sensitive` if the export or the ops touch secrets. `policy` makes the account close to admin (see [Security](07-security-and-observability.md#target-accounts)). Spike: the scheduler's behaviour across a reboot, and `/export` over REST (see [Apply](05-apply-and-rollout.md#routeros)).

## Proxmox

Proxmox is three kinds of target with different transports and scopes.

### Cluster target: SDN

**Transport.** The PVE API (`/cluster/sdn/...`), authenticated with an API token.

**Managed scope.** Everything under SDN: the controller (`evpnctl`), zones, VNets and subnets. sharpwire owns SDN completely, so any SDN object not in NetBox is removed.

**Live state.** The running SDN config. A plan is only valid if the **pending** SDN config is empty or exactly matches the plan's own staged ops. Changes someone staged in the GUI and never applied would otherwise ride along with the next apply.

**Apply.** Stage the ops through the API, re-read the pending config and compare it with the plan, then apply SDN (`PUT /cluster/sdn`). SDN apply is cluster-wide. It regenerates `interfaces.d/sdn` and `frr.conf` on every node, merging each node's `frr.conf.local`.

| Object | Phase 1 | Phase 2 |
| --- | --- | --- |
| Controller `evpnctl` | ASN 65000, peers = node loopbacks | + gateway VTEPs |
| Zones | `public` (L3VNI 20000, `advertise-subnets`, MTU, `ipam pve`, no exit nodes, no SNAT) | + `cNN` per customer |
| VNets and subnets | public VNets, tags from the public range, subnets with gateways | + `cNNnXX` VNets and their subnets |

### Cluster target: cluster files

**Read-only in v1.** sharpwire reads these files and reports drift; apply comes **(later)**. In v1, corosync link1 is added by hand once, following the network doc's Step 3.

**Transport.** The SSH helper on one healthy node, writing to `/etc/pve` (pmxcfs replicates the files).

**Managed scope.**
- `/etc/pve/corosync.conf`: the nodelist, both links (`ring0_addr` on mgmt, `ring1_addr` on the VTEP loopback) and their `knet_link_priority`, with link0 preferred. sharpwire bumps `config_version` itself.
- `/etc/pve/datacenter.cfg`: only the keys sharpwire owns (`migration`). Other keys are left alone.

Corosync changes are protected and always need a human. Validation rejects any plan that changes more than one link, or that changes a link while the other one is down. The first corosync change in phase 1 adds link1, which only adds a link and leaves link0 alone.

### Cluster target: firewall

**Read-only in v1, and only the `enable` option.** v1 checks that the cluster firewall is on and runs the isolation cross-check below. Modelling and applying rules comes **(later)**.

**Transport.** The PVE API: `/cluster/firewall/options` (v1). Later also `/cluster/firewall/rules`, `/cluster/firewall/groups` and `/nodes/<node>/firewall/rules`.

**Managed scope.**
- v1: the cluster firewall `enable` option.
- **(later)** the default policies; the cluster rules and security groups that sharpwire defines; each node's host rules, including allowing BGP (TCP 179), BFD (UDP 3784-3785, 4784) and VXLAN (UDP 4789) on VLANs 101 and 102.

**Not in scope.** Per-VM firewall config and the cluster ipsets belong to sharpstack, and are never part of the diff.

**Isolation cross-check (v1).** Public-zone isolation depends on the firewall being on at every level. sharpwire raises a **critical** alert when:
- the cluster firewall is disabled;
- a NIC on a public VNet doesn't have `firewall=1`;
- such a VM's own firewall (`/nodes/<n>/qemu/<id>/firewall/options`) doesn't have `enable=1` and `ipfilter=1`.

sharpwire reads guest NIC and firewall options for this check (it needs `VM.Audit`); it never writes them. While the cluster firewall is disabled, sharpwire refuses to set any public VNet `active`.

### Node targets: host files

**Transport.** SSH as a dedicated `sharpwire` user. Sudo is restricted to one installed helper, `sharpwire-node`, which reads and writes the managed files, validates, arms and disarms the rollback, and reloads services. sharpwire ships a versioned bootstrap package that creates the user, installs the helper, the sudoers rule and `sharpwire-restore.service`, and authorizes the controller's key. An operator runs it once per node during the manual bootstrap (see [Adding a Proxmox node](05-apply-and-rollout.md#adding-a-proxmox-node)). The helper upgrades by re-running a newer bootstrap package; sharpwire doesn't push its own helper. The controller reads the helper version and refuses to plan for a node whose helper is older than it supports.

**Managed scope.** Rendered as whole files and diffed by stanza:

| File | Contents (network doc reference) |
| --- | --- |
| `/etc/network/interfaces` | `lo`, NICs, `bond0`, `bond0.10` (including the corosync `egress-qos-map`, Step 3), `bond0.101`, `bond0.102`, `dummy0`, `bond0.300` in `vrf_public` (Steps 2 and 5c), and in phase 2 the restricted VyOS bridge on hosting nodes. `interfaces.d/sdn` is SDN's and is never touched |
| `/etc/sysctl.d/90-sdn.conf` | forwarding and RA settings (Step 2) |
| `/etc/frr/frr.conf.local` | BFD profiles, `UL-IN`/`UL-OUT`, the underlay in `router bgp 65000`, the `vrf_public` handoff (`CORE`/`CORE6`, `PUB-OUT`, `DEFAULT-ONLY`, `NO-DEFAULT`) (Steps 4a and 5c) |
| `/etc/hosts` | all cluster nodes' mgmt addresses (Step 3) |

**Apply.**
- Interfaces: `ifreload -a -n` as a dry run, then `ifreload -a`.
- sysctl: `sysctl --system`.
- `frr.conf.local`: regenerate the node's `frr.conf` the way SDN apply does, then reload FRR. The exact per-node mechanism is a spike item.

**Ordering constraint.** `bond0.300` must be enslaved to `vrf_public`, which exists only after SDN has been applied on that node. So creating the public zone or adding a node always applies SDN before the node's `bond0.300` change (see [Change set stages](05-apply-and-rollout.md#change-set-stages)). Validation rejects a node plan that references `vrf_public` while the node has no generated `vrf_public`.

**Post-merge check.** After any SDN apply or `frr.conf.local` change, the helper reads `vtysh -c 'show run'`. The health check confirms that the merged config contains the rendered router-id, `update-source dummy0` on the `VTEP` peer group, and `NO-DEFAULT` on the `vrf_public` advertise lines.

## VyOS (phase 2)

**Transport.** The VyOS HTTP API, applied with commit-confirm.

**Managed scope.** The whole config tree, apart from the baseline: the system login used by sharpwire, physical interface hardware IDs and dynamic entries.

**Models.**
- Shared: `eth1` and VLANs 101, 102 and 310, `dum0`, BFD profiles, the underlay BGP with `local-as`, EVPN iBGP to every node loopback and to the other gateway, the `outside` VRF with blackholed aggregates and eBGP to both cores, and the active/standby policy (`EVPN-BACKUP` and the AS-path prepend on the standby only).
- Conntrack: per-VRF conntrack zones, plus optional `conntrack-sync`.
- Per customer: the network doc's B.5 template, with every name and number derived from NN.

**Version requirement.** The driver refuses to plan for a second customer with an overlapping range unless the running version is on a list that has been verified to separate conntrack per VRF.
