# 9. sharpstack integration

Nov 20, 2026 · Draft

How sharpwire and sharpstack (`../sharpstack`) divide the work, what sharpstack must change before sharpwire v1 goes live, and what sharpwire reuses. sharpstack has no live customers and no deployed VPCs, so its networking code is changeable and nothing needs migrating. References below name files and functions rather than line numbers, so they don't go stale.

## Ownership

| Area | Owner |
| --- | --- |
| Proxmox SDN, VNI allocation and VNet naming | sharpwire only. sharpstack never calls `/cluster/sdn` |
| Proxmox node networking, FRR, cluster files, the cluster firewall `enable` | sharpwire |
| VNet subnet prefixes and their gateway IPs in NetBox | sharpwire |
| Per-VM firewall, ipfilter, cluster ipsets | sharpstack (`app/services/proxmox.py`) |
| PVE users, roles, ACLs | sharpstack |
| Guests: VMs, NICs, cloud-init | sharpstack |
| NetBox tenants, VMs, their interfaces and IPs | sharpstack, tagged `sharpstack-managed` |
| NetBox devices, cabling, fabric VLANs, prefixes, ASNs, VRFs and the plugin objects | sharpwire, tagged `sharpwire-managed` |
| MikroTik cores and leafs | sharpwire |

## Public network handoff (v1)

Public VNets are **shared platform capacity**, as sharpstack already treats them: it picks public subnets by headroom (`app/services/ipam.py`). Customers are isolated by the PVE firewall.

1. An admin creates the VNet and its subnets in NetBox ([Intent model](02-intent-model.md#netbox-mapping)).
2. sharpwire plans the change. A person approves it, and sharpwire applies it and sets the VNet's `status` to `active`.
3. An admin links the VNet in sharpstack. `PlatformSubnet` points at the plugin VNet instead of a NetBox VLAN: the bridge is `vnetNNN` (untagged), the prefixes come from the VNet's subnets, and the gateway is the IP sharpwire created.
4. sharpstack uses only `active` VNets.

## sharpstack prerequisites for sharpwire v1

These must be done before sharpwire applies SDN.

| # | Change in sharpstack |
| --- | --- |
| 1 | Remove all SDN writes: `create_sdn_vnet`, `create_sdn_subnet`, `delete_sdn_vnet`, `apply_sdn_config` (`app/services/proxmox.py`) and their callers (`app/tasks/subnet.py`) |
| 2 | Remove its own VNI allocation: `vni_range`, `allocate_platform_vni` (`app/services/ipam.py`) and `vnet_id` (`app/services/proxmox_config.py`) |
| 3 | `PlatformSubnet` points at a sharpwire VNet, uses it only when it's `active`, and reuses sharpwire's gateway IP instead of reserving network+1 itself |
| 4 | Accept prefixes in a VRF (`app/api/platform_subnets.py`, and the no-VRF rule in its `AGENTS.md`) |
| 5 | Reach the IPv6-only mgmt network: enable IPv6 on the compose network (`docker-compose.prod.yml`). `ssh_node_host_overrides` then holds IPv6 literals |
| 6 | Remove `SDN.*` rights from sharpstack's PVE token, apart from `SDN.Use` on the zones it attaches NICs to. Add a preflight check that fails if the token can still allocate SDN (`app/services/platform_preflight.py`) |
| 7 | Keep the `vpc` feature off for every organization until phase 2 (`app/services/features.py`) |
| 8 | Drop the hand-built `evpn` and `vlan` zones from `scripts/integration_lab_setup.sh`, and run the integration lab on SDN applied by sharpwire |

## Reused from sharpstack

- **Stack and conventions:** Python 3.14, FastAPI, async SQLAlchemy with asyncpg, Alembic, pydantic-settings, ruff, mypy, uv, and pytest against a real Postgres. NetBox 4.7. The naming, error and config rules in sharpstack's `AGENTS.md` (see sharpwire's own `AGENTS.md`).
- **OIDC:** `app/services/oidc.py`, plus groups-claim handling.
- **Audit log:** the `audit_logs` shape and secret redaction.
- **Alerts:** the outbox's dedup key and topic semantics.

Deliberate differences:
- **A Postgres job queue instead of Celery and Redis.** There is one controller with low volume, and the apply gate lives with the plans.
- **A server-rendered operator UI instead of a SPA.** sharpstack's SPA is customer-facing.
- **Kanidm for staff login.** sharpstack's staff use passkeys.

## Deployment

sharpwire runs as its own compose project with its own Postgres, behind the same outer TLS proxy as sharpstack. Its container network must be IPv6-enabled, because every target's mgmt address is IPv6.

Phase 2 (sharpstack's VPC model) is in [later](later.md#sharpstacks-vpc-model).
