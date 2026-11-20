"""The intent builder, from a real NetBox 4.7.5 GraphQL snapshot (tests/fixtures/netbox/example_network.json)."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.drivers.routeros.render import render_core, render_leaf
from app.drivers.routeros.resources import to_rsc
from app.intent.builder import BuildError, build_network, natural_key
from tests.fixtures.example_network import example_network
from tests.fixtures.netbox import load

GOLDEN = Path(__file__).parent.parent / "golden" / "routeros"

Snapshot = dict[str, Any]


def snapshot() -> Snapshot:
    return copy.deepcopy(load("example_network"))


def device(s: Snapshot, name: str) -> dict[str, Any]:
    return next(d for d in s["device_list"] if d["name"] == name)


def interface(s: Snapshot, device_name: str, name: str) -> dict[str, Any]:
    return next(i for i in device(s, device_name)["interfaces"] if i["name"] == name)


def problems(s: Snapshot) -> list[str]:
    with pytest.raises(BuildError) as exc:
        build_network(s)
    return exc.value.problems


def test_builds_the_example_network() -> None:
    assert build_network(snapshot()) == example_network()


@pytest.mark.parametrize("name", ["core-a", "core-b", "leaf-a"])
def test_netbox_renders_the_golden_config(name: str) -> None:
    network = build_network(snapshot())
    if name.startswith("core"):
        rendered = to_rsc(render_core(network, network.core_by_name[name]))
    else:
        rendered = to_rsc(render_leaf(network, next(lf for lf in network.leafs if lf.name == name)))
    assert rendered == (GOLDEN / f"{name}.rsc").read_text()


def test_input_order_does_not_matter() -> None:
    s = snapshot()
    s["device_list"].reverse()
    for d in s["device_list"]:
        d["interfaces"].reverse()
        for i in d["interfaces"]:
            i["ip_addresses"].reverse()
            i["tagged_vlans"].reverse()
    for key in ("vlan_list", "vlan_prefixes", "role_prefixes", "asn_list"):
        s[key].reverse()
    assert build_network(s) == example_network()


def test_natural_key() -> None:
    names = ["sfp-sfpplus13", "sfp-sfpplus2", "sfp-sfpplus1"]
    assert sorted(names, key=natural_key) == ["sfp-sfpplus1", "sfp-sfpplus2", "sfp-sfpplus13"]


def _drop_asn_tag(slug: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        for a in s["asn_list"]:
            a["tags"] = [t for t in a["tags"] if t["slug"] != slug]

    return mutate


def _set_cf(device_name: str, key: str, value: Any, iface: str | None = None) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        obj = interface(s, device_name, iface) if iface else device(s, device_name)
        obj["custom_fields"][key] = value

    return mutate


def _remove_interface(device_name: str, name: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        d = device(s, device_name)
        d["interfaces"] = [i for i in d["interfaces"] if i["name"] != name]

    return mutate


def _set_platform(device_name: str, slug: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        device(s, device_name)["platform"] = {"slug": slug}

    return mutate


def _drop_vlans(role: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        s["vlan_list"] = [v for v in s["vlan_list"] if v["role"]["slug"] != role]

    return mutate


def _drop_role_prefixes(role: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        s["role_prefixes"] = [p for p in s["role_prefixes"] if p["role"]["slug"] != role]

    return mutate


def _drop_vlan_prefix(prefix: str) -> Callable[[Snapshot], None]:
    def mutate(s: Snapshot) -> None:
        s["vlan_prefixes"] = [p for p in s["vlan_prefixes"] if p["prefix"] != prefix]

    return mutate


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_drop_asn_tag("overlay"), "Expected one ASN tagged overlay, found none."),
        (_drop_asn_tag("public-handoff"), "Expected one ASN tagged public-handoff, found none."),
        (_drop_vlans("mgmt"), "Expected one VLAN with role mgmt, found none."),
        (_drop_vlans("underlay"), "No VLAN has role underlay. Add one per core."),
        (_drop_role_prefixes("vtep"), "Expected one prefix with role vtep, found none."),
        (_drop_role_prefixes("public"), "No prefix has role public. Add the public ranges."),
        (_drop_vlan_prefix("2001:db8:101::/64"), "Expected one IPv6 prefix on underlay VLAN 101, found none."),
        (_drop_vlan_prefix("10.250.0.0/24"), "Expected one IPv4 prefix on handoff VLAN 300, found none."),
        (_set_cf("core-a", "asn", None), "core-a: set the asn custom field."),
        (_set_cf("pve2", "router_id", None), "pve2: set the router_id custom field."),
        (_set_cf("leaf-a", "l2mtu", None, iface="sfp-sfpplus1"), "leaf-a: set the MTU and the l2mtu custom field"),
        (
            _remove_interface("pve3", "dummy0"),
            "pve3: a node needs exactly one active IPv6 address on dummy0, found none.",
        ),
        (_remove_interface("leaf-b", "bridge"), "leaf-b: a leaf needs exactly one bridge interface, found none."),
        (_remove_interface("core-b", "bond-leafs"), "core-b: a core needs exactly one LAG, found none."),
        (_set_platform("core-a", "proxmox"), "core-a has role core, so its platform must be routeros."),
    ],
)
def test_says_what_to_fix_in_netbox(mutate: Callable[[Snapshot], None], expected: str) -> None:
    s = snapshot()
    mutate(s)
    assert any(p.startswith(expected) for p in problems(s)), problems(s)


def test_public_prefix_needs_max_subnet_length() -> None:
    s = snapshot()
    next(p for p in s["role_prefixes"] if p["prefix"] == "198.51.100.0/24")["custom_fields"]["max_subnet_length"] = None
    assert "Public prefix 198.51.100.0/24 has no max_subnet_length. Set the custom field." in problems(s)


def test_reports_every_problem_at_once() -> None:
    s = snapshot()
    _set_cf("core-a", "asn", None)(s)
    _set_cf("pve1", "router_id", None)(s)
    _drop_asn_tag("overlay")(s)
    assert len(problems(s)) == 3


def test_cross_target_rules_still_apply() -> None:
    # Two nodes with the same ASN: NetBox is complete, but the network isn't valid.
    s = snapshot()
    device(s, "pve2")["custom_fields"]["asn"] = device(s, "pve1")["custom_fields"]["asn"]
    assert any("is used by 2 nodes" in p for p in problems(s))


def test_a_bad_router_id_is_reported() -> None:
    s = snapshot()
    _set_cf("core-b", "router_id", "10.255.255")(s)
    assert "core-b: router_id '10.255.255' isn't an IPv4 address." in problems(s)


def test_a_core_vlan_interface_off_the_lag_is_reported() -> None:
    s = snapshot()
    interface(s, "core-a", "vlan300")["parent"] = None
    assert "core-a: set the parent of vlan300 to LAG bond-leafs." in problems(s)


def test_a_leaf_without_a_peer_link_is_rejected() -> None:
    s = snapshot()
    for name in ("sfp-sfpplus15", "sfp-sfpplus16"):
        interface(s, "leaf-a", name)["connected_endpoints"] = []
    assert "leaf-a: a leaf needs exactly one LAG cabled to the other leaf, found none." in problems(s)


def add_address(s: Snapshot, device_name: str, iface: str, address: str, status: str = "active") -> None:
    interface(s, device_name, iface)["ip_addresses"].append({"address": address, "status": status})


def test_only_active_addresses_count() -> None:
    s = snapshot()
    add_address(s, "pve1", "bond0.101", "2001:db8:101::5/64", status="deprecated")
    add_address(s, "core-a", "vlan101", "2001:db8:101::99/64", status="reserved")
    assert build_network(s) == example_network()


def test_a_second_active_address_is_reported_not_picked() -> None:
    s = snapshot()
    add_address(s, "pve1", "bond0.101", "2001:db8:101::5/64")
    assert "pve1: bond0.101 has 2 active IPv6 addresses. Keep one active." in problems(s)


def test_an_interface_on_a_vlan_outside_the_fabric_is_reported() -> None:
    # Another site's VLAN 102, not tagged sharpwire-managed: same VID, different VLAN.
    s = snapshot()
    interface(s, "pve1", "bond0.102")["untagged_vlan"] = {"id": "999", "vid": 102}
    interface(s, "leaf-a", "bond-pve1")["tagged_vlans"].append({"id": "999", "vid": 102})
    found = problems(s)
    assert "pve1: bond0.102 uses VLAN 102, which isn't a fabric VLAN tagged sharpwire-managed." in found
    assert "leaf-a: bond-pve1 uses VLAN 102, which isn't a fabric VLAN tagged sharpwire-managed." in found


def test_two_fabric_vlans_with_one_vid_are_reported() -> None:
    s = snapshot()
    s["vlan_list"].append({"id": "999", "vid": 101, "name": "other", "role": {"slug": "underlay"}})
    assert "2 fabric VLANs have ID 101. Tag only one of them sharpwire-managed." in problems(s)


def test_a_prefix_whose_vlan_is_hidden_is_reported() -> None:
    s = snapshot()
    s["vlan_prefixes"][0]["vlan"] = None
    assert any("VLAN isn't visible. Let the NetBox token view VLANs." in p for p in problems(s))


def test_overlapping_public_ranges_are_reported() -> None:
    s = snapshot()
    s["role_prefixes"].append(
        {"prefix": "198.51.100.0/25", "role": {"slug": "public"}, "custom_fields": {"max_subnet_length": 28}}
    )
    assert "Public prefix 198.51.100.0/25 is inside public prefix 198.51.100.0/24. Keep only one." in problems(s)


def test_an_unnamed_device_is_reported() -> None:
    s = snapshot()
    device(s, "leaf-b")["name"] = None
    assert "A device with role leaf has no name. Name it." in problems(s)


def test_an_asn_outside_the_fabric_is_reported() -> None:
    s = snapshot()
    device(s, "pve1")["custom_fields"]["asn"] = 99999
    assert "pve1: its asn custom field points at an ASN that isn't tagged sharpwire-managed." in problems(s)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_remove_interface("core-a", "vlan300"), "Core core-a has no interface on handoff VLAN 300."),
        (_remove_interface("core-a", "vlan10"), "Core core-a has no interface on mgmt VLAN 10."),
    ],
)
def test_a_core_missing_an_interface_is_reported(mutate: Callable[[Snapshot], None], expected: str) -> None:
    s = snapshot()
    mutate(s)
    assert expected in problems(s)


def test_a_core_missing_an_address_is_reported() -> None:
    s = snapshot()
    pub = interface(s, "core-a", "vlan300")
    pub["ip_addresses"] = [a for a in pub["ip_addresses"] if ":" in a["address"]]
    interface(s, "core-b", "vlan102")["ip_addresses"] = []
    found = problems(s)
    assert "Core core-a's vlan300 needs an IPv4 and an IPv6 address." in found
    assert "Core core-b's vlan102 needs an IPv6 address on underlay VLAN 102." in found


def test_a_leaf_mgmt_off_the_mgmt_vlan_is_reported() -> None:
    s = snapshot()
    mgmt = interface(s, "leaf-a", "mgmt")
    mgmt["untagged_vlan"] = interface(s, "core-a", "vlan101")["untagged_vlan"]
    assert "Leaf leaf-a's mgmt must be on mgmt VLAN 10." in problems(s)


def test_a_lag_must_be_tagged() -> None:
    s = snapshot()
    interface(s, "leaf-a", "bond-pve1")["mode"] = "access"
    assert "leaf-a: set the 802.1Q mode of LAG bond-pve1 to tagged." in problems(s)


def test_a_bond_must_reach_one_device() -> None:
    s = snapshot()
    interface(s, "leaf-a", "sfp-sfpplus1")["connected_endpoints"] = []
    assert "leaf-a: LAG bond-pve1's members must connect to one device; they reach no device." in problems(s)


def test_a_lag_without_mtu_is_reported_once() -> None:
    s = snapshot()
    interface(s, "core-a", "bond-leafs")["mtu"] = None
    assert [p for p in problems(s) if p.startswith("core-a")] == ["core-a: set the MTU of LAG bond-leafs."]


def test_every_cross_target_problem_is_its_own_sentence() -> None:
    s = snapshot()
    device(s, "pve2")["custom_fields"]["asn"] = device(s, "pve1")["custom_fields"]["asn"]
    _remove_interface("core-a", "vlan10")(s)
    found = problems(s)
    assert len(found) >= 2
    assert all("\n" not in p for p in found)


def test_reads_sharpwire_managed() -> None:
    s = snapshot()
    device(s, "pve3")["custom_fields"]["sharpwire_managed"] = None
    network = build_network(s)
    assert [n.managed for n in network.nodes] == [True, True, False]
