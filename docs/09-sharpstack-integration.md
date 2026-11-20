# 9. sharpstack integration

Nov 20, 2026 · Draft

How sharpwire and sharpstack (`../sharpstack`) divide the work, what sharpstack must change before sharpwire v1 goes live, and what sharpwire reuses from it. This doc comes from a review of the sharpstack codebase on Nov 20, 2026. sharpstack has no live customers and no deployed VPCs, so its current networking code is treated as changeable and nothing needs migrating.

## Ownership

| Area | Owner | Notes |
| --- | --- | --- |
| Proxmox SDN (controller, zones, VNets, subnets, SDN apply) | **sharpwire only** | sharpstack never calls `/cluster/sdn` |
| VNI allocation and VNet naming | **sharpwire** (NetBox plugin) | In v1 a person allocates in NetBox; the plugin's allocation call comes later |
| Proxmox node networking, FRR, cluster files | sharpwire | |
| Cluster-level PVE firewall: `enable` (v1), cluster and host rules (later) | sharpwire | Drift-only in v1 (see [Drivers](04-drivers.md#cluster-target-firewall)) |
| VNet subnet prefixes and their gateway IP in NetBox | sharpwire | sharpwire creates the gateway IP (first address, role anycast); sharpstack reuses it instead of reserving its own |
| Per-VM firewall, ipfilter, cluster ipsets | sharpstack | `app/services/proxmox.py:485-557` |
| PVE users, roles, ACLs (consoles, later `SDN.Use` on `cNN` zones) | sharpstack | `app/services/proxmox.py:433-470` |
| Guests: VMs, NICs, cloud-init | sharpstack | |
| NetBox: tenants, sites, clusters, VMs, interfaces, IPs, IP-level prefixes | sharpstack | Tagged `sharpstack-managed` (`app/services/netbox.py:22`) |
| NetBox: sharpwire plugin objects (zones, VNets, BGP, policy, redundancy groups), devices, cabling, VRFs, L2VPNs, ASNs, and the public aggregates | sharpwire | Tagged `sharpwire-managed` |
| MikroTik cores and leafs | sharpwire | sharpstack has no RouterOS client (`docs/TODO.md:28`) |

NetBox event rules that trigger sharpwire plans fire only on sharpwire's objects. sharpstack's constant VM and IP writes never trigger a plan.

## Public network handoff (v1)

Public VNets are **shared platform capacity**, matching sharpstack's current model, where it picks public subnets by headroom (`app/services/ipam.py:31-89`). Customers are isolated by the PVE firewall, not by separate VNets.

1. An admin creates the VNet and its subnets in NetBox: tag and name `vnetNNN` from 200–999, prefixes inside the public aggregates, in VRF `vrf_public`.
2. sharpwire plans the SDN change, a human approves it, and sharpwire applies it and sets the plugin VNet's `status` from `planned` to `active`.
3. An admin links the VNet in sharpstack. `PlatformSubnet` references the **plugin VNet** instead of a NetBox VLAN, with:
   - `proxmox_bridge = vnetNNN`, untagged;
   - the IPv6 /64 and the IPv4 prefix taken from the VNet's subnets;
   - the gateway: the first address, already created in NetBox by sharpwire. sharpstack stops reserving network+1 itself for these subnets (`app/services/ipam.py:25-28`, `app/tasks/subnet.py:39-46`).
4. sharpstack only lets VMs use VNets whose status is `active`.

sharpwire creates no NetBox VLAN object for VNets. A VNet is not an 802.1Q VLAN.

Creating instances and attaching NICs take addresses from NetBox and need no sharpwire plan. So v1's human approval only gates adding public capacity, which is already an admin-only action in sharpstack.

## sharpstack prerequisites for sharpwire v1

These are changes to sharpstack. sharpwire v1 must not apply SDN on a platform until they're done.

| # | Change | Where |
| --- | --- | --- |
| 1 | Remove all SDN writes: `create_sdn_vnet`, `create_sdn_subnet`, `delete_sdn_vnet`, `apply_sdn_config`, and their callers | `app/services/proxmox.py:639-671`, `app/tasks/subnet.py:97-105,127` |
| 2 | Remove sharpstack's own VNI allocation (`vni_range`, `allocate_platform_vni`, `vnet_id`) | `app/services/ipam.py:95-119`, `app/services/proxmox_config.py:172-174` |
| 3 | Make `PlatformSubnet` reference a sharpwire VNet, gate on its `active` status, and reuse sharpwire's gateway IP (see the handoff above) | `app/models/platform_subnet.py`, `app/api/platform_subnets.py` |
| 4 | Accept prefixes in a VRF (`vrf_public`) | `app/api/platform_subnets.py:83-88`, rule in sharpstack `AGENTS.md:219` |
| 5 | Reach the IPv6-only management network: enable IPv6 on the compose network, or use a dual-stack path into VLAN 10. `ssh_node_host_overrides` then holds IPv6 literals | `docker-compose.prod.yml:176-183` |
| 6 | Drop every `SDN.*` privilege from sharpstack's PVE token, apart from `SDN.Use` on the zones it attaches NICs to, and add a preflight check that fails if the token still has `SDN.Allocate` (today's preflight only checks backup permissions) | the operator-made PVE role; `app/services/platform_preflight.py` |
| 7 | Keep the `vpc` feature off for every organization until phase 2. The flag is per organization, not per platform | `app/services/features.py:22`, `docs/operator/platform-setup.md:24-25` |
| 8 | Delete the hand-built `evpn` and `vlan` zones from the lab setup and point sharpstack's integration lab at SDN applied by sharpwire | `scripts/integration_lab_setup.sh:66-90` |

## Reused from sharpstack

sharpwire is a sibling project for the same operator, so it follows sharpstack's conventions unless there's a reason not to.

- **Stack baseline:** Python 3.14, FastAPI, async SQLAlchemy 2 with asyncpg, Alembic (one revision per change), pydantic-settings, ruff, mypy, uv, and pytest against a real Postgres. NetBox is pinned to 4.7, as in sharpstack's compose files.
- **Conventions:** the naming, error (`{detail, error_code}`), config-refuses-to-start and layering rules from sharpstack's `AGENTS.md`. sharpwire gets its own `AGENTS.md` that starts from these.
- **OIDC:** port `app/services/oidc.py`. It already does PKCE, state and nonce, chooses the algorithm from the JWK `kty` (ES256 for Kanidm), and pins discovery endpoints. Add groups-claim → role mapping.
- **Audit log:** mirror `audit_logs`. Rows are written at the API layer with IP, user agent and session, and `snapshot(secret_fields=)` redacts secrets.
- **Alerts:** the `alerts` table takes a dedup key and a topic with supersede and withdraw semantics, as in sharpstack's notification outbox.
- **Locking:** sharpstack's lock-ordering rule. The per-target apply lock is session-level (`pg_advisory_lock`) rather than sharpstack's transaction-scoped `advisory_xact_lock`, because an apply lasts minutes.

**Deliberate differences:**
- **Job queue.** A Postgres job queue (`SKIP LOCKED`) instead of Celery and Redis. One controller and low job volume don't justify Redis, and the apply gate (locks, and later freezes) lives in the same database as the plans. sharpstack keeps Redis because it also backs rate limiting, its NetBox cache and passkey challenges.
- **UI.** A server-rendered operator UI instead of a SPA. sharpstack's SPA is customer-facing, and sharpwire has about six operator screens.
- **Staff login.** sharpwire uses Kanidm. sharpstack's staff log in with passkeys. Giving sharpstack's platform admins Kanidm login too, through the same `oidc.py`, would give operators one identity; that's open.

## Deployment

sharpwire runs as its own compose project, with its own Postgres, behind the same outer TLS proxy as sharpstack. It doesn't share a database with sharpstack. Its container network must be IPv6-enabled, because every target's management address is IPv6.

## Phase 2: VPC model

sharpstack's current VPC model doesn't match the network design:

| | sharpstack today | sharpwire / network doc |
| --- | --- | --- |
| Zone | One shared zone, no VRF | One zone, VRF and L3VNI per customer (`cNN`) |
| Per tenant | Several VPCs | One customer zone |
| IPv6 | /48 per VPC, /64 per subnet | /56 per customer, /64 per VNet |
| IPv4 | One platform pool, no overlap | Private, overlap allowed, one NAT IPv4 per customer |
| Limit | none | 99 customers (NN 1–99) |

sharpstack's own TODO says the fabric "needs to be designed from scratch" (`docs/TODO.md:19-29`). The mapping is an open question, to be settled before phase 2 starts.
