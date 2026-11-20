"""Reading intent from NetBox's GraphQL API (NetBox 4.7, with the sharpwire plugin's custom fields, roles and tags).

The intent builder (`app/intent/builder.py`) reads a snapshot: one list per kind of object, saved as is for test
fixtures (tests/fixtures/netbox/). Only fabric objects are read: those tagged `sharpwire-managed`, and of devices,
VLANs and prefixes only the active ones. NetBox caps every list at MAX_PAGE_SIZE, so each list is read page by page.

The lists are separate requests, so an edit in NetBox while they're read can land between them. A plan's stale check
(docs/03-pipeline-and-validation.md) catches that.

The token needs view permission on: devices, interfaces, device roles, platforms, cables, VLANs, prefixes,
IP addresses, ASNs, IPAM roles and tags. Without it, NetBox hides objects instead of failing, and the build reports
them as missing.
"""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TAG = "sharpwire-managed"
DEVICE_ROLES = ("core", "leaf", "pve-node")
VLAN_ROLES = ("mgmt", "underlay", "public-handoff")
VLAN_PREFIX_ROLES = ("underlay", "public-handoff")
PREFIX_ROLES = ("vtep", "public")


def _strings(values: tuple[str, ...]) -> str:
    """A GraphQL list of strings (JSON's syntax is the same)."""
    return json.dumps(list(values))


# What each list asks NetBox for. {page} is replaced by the pagination argument.
_TAGGED = f'tags: {{slug: {{exact: "{TAG}"}}}}'
_ACTIVE = "status: {exact: STATUS_ACTIVE}"

LIST_QUERIES = {
    "device_list": f"""
  device_list(filters: {{role: {{slug: {{in_list: {_strings(DEVICE_ROLES)}}}}}, {_ACTIVE}, {_TAGGED}}}, {{page}}) {{
    name
    role {{ slug }}
    platform {{ slug }}
    custom_fields
    interfaces {{
      name
      type
      mtu
      mode
      custom_fields
      lag {{ name }}
      parent {{ name }}
      untagged_vlan {{ id vid }}
      tagged_vlans {{ id vid }}
      ip_addresses {{ address status }}
      connected_endpoints {{ ... on InterfaceType {{ device {{ name }} }} }}
    }}
  }}""",
    "vlan_list": f"""
  vlan_list(filters: {{role: {{slug: {{in_list: {_strings(VLAN_ROLES)}}}}}, {_ACTIVE}, {_TAGGED}}}, {{page}}) {{
    id
    vid
    name
    role {{ slug }}
  }}""",
    "vlan_prefixes": f"""
  vlan_prefixes: prefix_list(
    filters: {{vlan: {{role: {{slug: {{in_list: {_strings(VLAN_PREFIX_ROLES)}}}}}}}, {_ACTIVE}, {_TAGGED}}}, {{page}}
  ) {{
    prefix
    vlan {{ id vid }}
  }}""",
    "role_prefixes": f"""
  role_prefixes: prefix_list(
    filters: {{role: {{slug: {{in_list: {_strings(PREFIX_ROLES)}}}}}, {_ACTIVE}, {_TAGGED}}}, {{page}}
  ) {{
    prefix
    role {{ slug }}
    custom_fields
  }}""",
    "asn_list": f"""
  asn_list(filters: {{{_TAGGED}}}, {{page}}) {{
    id
    asn
    tags {{ slug }}
  }}""",
}

PAGE_SIZE = 1000


class NetBoxError(Exception):
    """NetBox couldn't be read. The message says what to check; the cause is chained."""


def list_query(key: str, offset: int) -> str:
    page = f"pagination: {{offset: {offset}, limit: {PAGE_SIZE}}}"
    return "{" + LIST_QUERIES[key].replace("{page}", page) + "\n}"


async def fetch_intent_snapshot(
    netbox_url: str, netbox_token: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Every list the intent builder reads: the input of `app.intent.builder.build_network()`.

    The token is a NetBox 4.7 v2 token (`nbt_<key>.<secret>`), sent as a Bearer token. It's never logged.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=60) as own:
            return await _fetch(netbox_url, netbox_token, own)
    return await _fetch(netbox_url, netbox_token, client)


async def _fetch(netbox_url: str, netbox_token: str, client: httpx.AsyncClient) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in LIST_QUERIES:
        rows: list[Any] = []
        # Page until NetBox returns an empty page: its MAX_PAGE_SIZE may be lower than PAGE_SIZE.
        while True:
            page = (await _post(netbox_url, netbox_token, client, list_query(key, len(rows)))).get(key)
            if not isinstance(page, list):
                raise NetBoxError("NetBox returned no data for the intent query.")
            if not page:
                break
            rows += page
        snapshot[key] = rows
    return snapshot


async def _post(netbox_url: str, netbox_token: str, client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    url = f"{netbox_url.rstrip('/')}/graphql/"
    headers = {"Authorization": f"Bearer {netbox_token}", "Accept": "application/json"}
    try:
        response = await client.post(url, json={"query": query}, headers=headers)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NetBox GraphQL request failed", exc_info=True)
        raise NetBoxError("NetBox's GraphQL API couldn't be read. Check NETBOX_URL and NETBOX_TOKEN.") from exc
    if not isinstance(body, dict):
        raise NetBoxError("NetBox's GraphQL API returned something unexpected. Check NETBOX_URL.")
    if body.get("errors"):
        logger.warning("NetBox GraphQL returned errors: %s", body["errors"])
        raise NetBoxError(
            "NetBox rejected the intent query. Check that it runs NetBox 4.7, "
            "and that the token can view what docs/02-intent-model.md lists."
        )
    data = body.get("data")
    if not isinstance(data, dict):
        raise NetBoxError("NetBox returned no data for the intent query.")
    return data
