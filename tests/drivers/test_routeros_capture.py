"""The RouterOS diff and client against core-a as a real RouterOS 7.25.1 (a CHR) returned it.

The fixture was applied and read with tests/fixtures/routeros/capture.py. On a CHR, ether2 and ether3 stood in for
the core's SFP+ ports; the capture maps them back.
"""

import copy
import json
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.drivers.routeros.client import (
    RouterOSClient,
    RouterOSError,
    UnsupportedVersion,
    base_url,
    version_supported,
)
from app.drivers.routeros.plan import CORE_MENUS, fingerprint, plan_core
from tests.drivers.routeros_device import link
from tests.fixtures.example_network import example_network

CAPTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "routeros" / "core-a.live.json").read_text())
NETWORK = example_network()
CORE_A = NETWORK.core_by_name["core-a"]


def live() -> dict[str, list[dict[str, str]]]:
    return copy.deepcopy(CAPTURE["live"])


def by(rows: list[dict[str, str]], **key: str) -> dict[str, str]:
    return next(r for r in rows if all(r.get(k.replace("_", "-")) == v for k, v in key.items()))


# --- The diff on a real device's rows ---------------------------------------------------------------------------


def test_the_capture_is_from_the_supported_version() -> None:
    assert version_supported(CAPTURE["version"])


def test_a_freshly_applied_core_differs_only_where_a_chr_cant_follow() -> None:
    draft = plan_core(NETWORK, CORE_A, live())

    # Virtio NICs have no l2mtu (spikes/routeros/FINDINGS.md); real SFP+ ports do.
    assert draft.diff == (
        "~ /interface/ethernet name=sfp-sfpplus1: l2mtu: (default) -> 9500\n"
        "~ /interface/ethernet name=sfp-sfpplus2: l2mtu: (default) -> 9500\n"
    )
    # The lab's own mgmt address on ether1, which sharpwire didn't make.
    assert [(u.path, u.key) for u in draft.unmanaged] == [
        ("ip/address", {"address": "192.168.88.1/24", "interface": "ether1"})
    ]
    # The SFP+ ports carry the mgmt VLAN, so an MTU-class change on them is a warning, not a guard.
    assert draft.guard_violations == ()
    assert [w.message for w in draft.warnings] == [
        "core-a: the mgmt path changes: the port sfp-sfpplus1, the port sfp-sfpplus2."
    ]


def test_the_capture_has_dynamic_rows_and_the_diff_ignores_them() -> None:
    rows = live()["ipv6/address"]
    assert any(r.get("dynamic") == "true" for r in rows)
    draft = plan_core(NETWORK, CORE_A, live())
    assert all(not op.path.startswith("ipv6") for op in draft.ops)
    # Neither changed nor reported: the dynamic link-locals aren't unmanaged rows either.
    assert all(u.path != "ipv6/address" for u in draft.unmanaged)


def test_on_a_real_device_the_last_rule_has_no_nextid() -> None:
    # RouterOS 7.25.1 leaves .nextid out on the last row; the order must still come from the chain, not the listing.
    rules = live()["routing/filter/rule"]
    assert ".nextid" not in rules[-1] and all(".nextid" in r for r in rules[:-1])
    from app.drivers.routeros.diff import linked_order

    assert linked_order(list(reversed(rules))) == rules


def test_real_drift_on_the_capture() -> None:
    state = live()
    by(state["routing/bgp/connection"], name="pub6")["use-bfd"] = "false"
    by(state["routing/bfd/configuration"], interfaces="vlan101,vlan300")["multiplier"] = "5"
    state["routing/filter/rule"] = [r for r in state["routing/filter/rule"] if r["chain"] != "ul-in-pve3"]
    link(state)

    draft = plan_core(NETWORK, CORE_A, state)

    assert [op.describe() for op in draft.ops if op.path != "interface/ethernet"] == [
        "~ /routing/bfd/configuration interfaces=vlan101,vlan300: multiplier: 5 -> 3",
        "+ /routing/filter/rule chain=ul-in-pve3 comment=sharpwire:ul-in-pve3:reject rule=reject",
        '+ /routing/filter/rule chain=ul-in-pve3 comment=sharpwire:ul-in-pve3:accept-loopback rule="if (dst == '
        '2001:db8:ffff::13/128) { accept }" (before chain=ul-in-pve3 comment=sharpwire:ul-in-pve3:reject)',
        "~ /routing/bgp/connection name=pub6: use-bfd: false -> yes",
    ]


def test_the_fingerprint_of_the_capture_is_stable() -> None:
    assert fingerprint(live(), CORE_MENUS) == fingerprint(live(), CORE_MENUS)


# --- The client ----------------------------------------------------------------------------------------------


def device(
    state: dict[str, Any], version: str = CAPTURE["version"], fail: dict[str, httpx.Response] | None = None
) -> httpx.MockTransport:
    """A device answering GETs from a capture, honouring `.proplist` the way RouterOS does."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the client never writes"
        path = urlparse(str(request.url)).path.removeprefix("/rest/")
        seen.append(path)
        if fail and path in fail:
            return fail[path]
        if path == "system/resource":
            return httpx.Response(200, json={"version": version, "board-name": "CHR"})
        if path not in state:
            return httpx.Response(
                400, json={"detail": "no such command prefix", "error": 400, "message": "Bad Request"}
            )
        wanted = parse_qs(urlparse(str(request.url)).query).get(".proplist", [""])[0].split(",")
        rows = state[path]
        trim = lambda r: {k: v for k, v in r.items() if k in wanted or k == ".id"}  # noqa: E731
        return httpx.Response(200, json=[trim(r) for r in rows] if isinstance(rows, list) else trim(rows))

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def client(transport: httpx.MockTransport) -> RouterOSClient:
    return RouterOSClient(
        "https://[2001:db8:100::2]:443/rest", "sharpwire", "secret", ssl.create_default_context(), transport=transport
    )


async def test_the_client_reads_what_the_diff_needs() -> None:
    transport = device(live())
    async with client(transport) as c:
        state = await c.read(CORE_MENUS)

    assert set(state) == set(CORE_MENUS)
    draft = plan_core(NETWORK, CORE_A, state)
    assert [op.path for op in draft.ops] == ["interface/ethernet", "interface/ethernet"]
    # Unmanaged fields aren't fetched: nothing sharpwire doesn't compare leaves the device.
    connections, rules = state["routing/bgp/connection"], state["routing/filter/rule"]
    assert isinstance(connections, list) and isinstance(rules, list)
    assert "routing-table" not in connections[0]
    assert ".nextid" in rules[0]


@pytest.mark.parametrize("version", ["7.24.6", "7.26", "7.2", "7.250", ""])
async def test_the_client_refuses_other_versions(version: str) -> None:
    transport = device(live(), version=version)
    async with client(transport) as c:
        with pytest.raises(UnsupportedVersion):
            await c.read(CORE_MENUS)
    assert transport.seen == ["system/resource"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", ["7.25", "7.25.1", "7.25.2 (stable)"])
def test_versions_on_the_supported_line(version: str) -> None:
    assert version_supported(version)


async def test_a_menu_the_device_lacks_is_an_error_with_routeros_detail() -> None:
    async with client(device(live())) as c:
        with pytest.raises(RouterOSError, match="interface/bridge/mlag: no such command prefix"):
            await c.read(["interface/bridge/mlag"])


async def test_a_refused_login_says_so_without_the_password() -> None:
    fail = {"system/resource": httpx.Response(401, json={"error": 401, "message": "Unauthorized"})}
    async with client(device(live(), fail=fail)) as c:
        with pytest.raises(RouterOSError) as exc:
            await c.read(CORE_MENUS)
    assert "refused sharpwire's login" in exc.value.detail
    assert "secret" not in exc.value.detail


async def test_an_unreachable_device_is_an_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with client(httpx.MockTransport(down)) as c:
        with pytest.raises(RouterOSError, match="didn't answer"):
            await c.version()


def test_base_urls_bracket_ipv6() -> None:
    from ipaddress import ip_address

    assert base_url(ip_address("2001:db8:100::2")) == "https://[2001:db8:100::2]:443/rest"
    assert base_url(ip_address("192.0.2.1"), 8443) == "https://192.0.2.1:8443/rest"


def test_tls_verification_cant_be_turned_off_and_proxies_are_ignored() -> None:
    with pytest.raises(TypeError):
        RouterOSClient("https://192.0.2.1/rest", "u", "p", False)  # type: ignore[arg-type]
    c = client(device(live()))
    assert c._http.trust_env is False


@pytest.mark.parametrize(
    ("path", "response", "message"),
    [
        ("system/resource", httpx.Response(200, text="<html>login</html>"), "something other than RouterOS JSON"),
        ("system/resource", httpx.Response(200, json=[]), "didn't say which RouterOS"),
        ("system/resource", httpx.Response(302, headers={"location": "/x"}), "something other than RouterOS JSON"),
        (
            "routing/bgp/connection",
            httpx.Response(400, json=["odd"]),
            "failed to read /routing/bgp/connection: HTTP 400",
        ),
        ("routing/bgp/connection", httpx.Response(200, json={"rows": [1]}), "a shape RouterOS doesn't use"),
        ("routing/bgp/connection", httpx.Response(200, json=[{"name": 5}]), "a shape RouterOS doesn't use"),
    ],
)
async def test_unexpected_answers_are_read_errors(path: str, response: httpx.Response, message: str) -> None:
    async with client(device(live(), fail={path: response})) as c:
        with pytest.raises(RouterOSError, match=message):
            await c.read(CORE_MENUS)
