# sharpwire NetBox plugin

sharpwire's part of NetBox ([intent model](../docs/02-intent-model.md#netbox-mapping)):

- **Zone**: an EVPN zone with its kind (`public` in v1), VRF, L3VNI and MTU.
- **VNet**: zone, VNI (also its VXLAN tag and its name, `vnet<VNI>`), subnets (prefixes in the zone's VRF) and
  `status` (`planned` or `active`). sharpwire sets `status` through the REST API. The UI forms don't edit it, and
  changing an active VNet's VNI, zone or subnets puts it back to `planned`.
- A prefix is a subnet of at most one VNet. While it is one, it can't be deleted or moved out of the zone's VRF.
- **Provisioning** after every `migrate` (`sharpwire_netbox/provisioning.py`). The custom fields `sharpwire_managed`,
  `asn`, `router_id`, `l2mtu`, `mlag_id`, `max_subnet_length` and `firewall_enabled`. The IPAM roles `mgmt`,
  `underlay`, `public-handoff`, `vtep` and `public`. The device roles `core`, `leaf` and `pve-node`. The platforms
  `routeros` and `proxmox`. The tags `overlay`, `public-handoff` and `sharpwire-managed`. It only adds what's
  missing, and logs a warning for anything it can't create as declared.

Both models have the usual UI views, REST endpoints (`/api/plugins/sharpwire/zones/`, `/api/plugins/sharpwire/vnets/`)
and GraphQL queries (`zone`, `zone_list`, `vnet`, `vnet_list`).

Built against NetBox 4.7 (see [COMPATIBILITY.md](COMPATIBILITY.md)). It has no dependencies beyond NetBox itself.

## Install

Into NetBox's own virtualenv, then enable it in `configuration.py`:

```sh
/opt/netbox/venv/bin/pip install sharpwire-netbox   # or the wheel from `uv build`
```

```python
PLUGINS = ["sharpwire_netbox"]
```

Then run NetBox's upgrade (`netbox upgrade`, or `upgrade.sh` for a git install). It applies the migrations and the
provisioning.

## Develop

NetBox 4.7 is on PyPI, so the dev environment installs the exact release from `pyproject.toml`. Tests use the
Postgres and Dragonfly from the repo's `docker-compose.yml`, through `testing/conf/configuration.py`:

```sh
docker compose up -d postgres redis      # from the repo root
cd netbox-plugin
uv sync
export NETBOX_ROOT=$PWD/testing
uv run netbox test sharpwire_netbox --keepdb
uv run netbox makemigrations sharpwire_netbox      # after a model change
uv run netbox migrate && uv run netbox runserver   # a local NetBox with the plugin, on the test config
```

Lint with the repo's ruff config: `uv run --project .. ruff check . && uv run --project .. ruff format --check .`
