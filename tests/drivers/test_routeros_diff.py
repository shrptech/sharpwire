"""The RouterOS diff and plans, against device views of the example network."""

import copy
from pathlib import Path

import pytest

from app.drivers.routeros import diff as routeros_diff
from app.drivers.routeros.diff import MENUS, LiveStateError, simulate, table_order
from app.drivers.routeros.fields import FIELDS, Field, normalise
from app.drivers.routeros.plan import CORE_MENUS, LEAF_MENUS, fingerprint, plan_core, plan_leaf
from app.drivers.routeros.render import render_core, render_leaf
from app.intent.models import Network
from app.schemas.plan import PlanDraft
from tests.drivers.routeros_device import device_view, link, row
from tests.fixtures.example_network import CORES, FABRIC, LEAFS, NODES, example_network, node

GOLDEN = Path(__file__).parent.parent / "golden" / "routeros" / "plans"
NETWORK = example_network()
CORE_A = NETWORK.core_by_name["core-a"]
LEAF_A = NETWORK.leafs[0]


def without_pve3() -> Network:
    leafs = tuple(
        leaf.model_copy(update={"bonds": tuple(b for b in leaf.bonds if b.name != "bond-pve3")}) for leaf in LEAFS
    )
    return Network(fabric=FABRIC, cores=CORES, leafs=leafs, nodes=NODES[:2])


def core_live(network: Network = NETWORK) -> dict[str, list[dict[str, str]]]:
    return device_view(render_core(network, network.core_by_name["core-a"]))


def leaf_live(network: Network = NETWORK) -> dict[str, list[dict[str, str]]]:
    return device_view(render_leaf(network, network.leafs[0]))


def assert_converges(draft: PlanDraft, network: Network, live: dict, *, leaf: bool = False) -> None:
    """Applying the plan leaves nothing to do, and applying its inverse after it gives back the original state."""
    menus = LEAF_MENUS if leaf else CORE_MENUS
    desired = render_leaf(network, network.leafs[0]) if leaf else render_core(network, network.core_by_name["core-a"])
    after = simulate(live, draft.ops, menus)
    assert routeros_diff.diff(desired, after, menus).ops == ()
    restored = simulate(after, draft.inverse_ops, menus)
    assert fingerprint(restored, menus) == fingerprint(live, menus)


# --- In sync -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["core-a", "core-b"])
def test_a_core_in_sync_has_an_empty_plan(name: str) -> None:
    core = NETWORK.core_by_name[name]
    draft = plan_core(NETWORK, core, device_view(render_core(NETWORK, core)))
    assert draft.empty
    assert draft.inverse_ops == ()
    assert draft.unmanaged == ()
    assert draft.warnings == () and draft.guard_violations == ()


@pytest.mark.parametrize("index", [0, 1])
def test_a_leaf_in_sync_has_an_empty_plan(index: int) -> None:
    leaf = NETWORK.leafs[index]
    assert plan_leaf(NETWORK, leaf, device_view(render_leaf(NETWORK, leaf))).empty


def test_equal_values_written_differently_are_in_sync() -> None:
    live = core_live()
    row(live, "routing/bfd/configuration", interfaces="vlan101,vlan300").update(
        {"interfaces": "vlan300,vlan101", "min-rx": "0.3s", "min-tx": "300ms"}
    )
    row(live, "routing/bgp/connection", name="ul-pve1")["remote.address"] = "2001:0db8:101:0:0:0:0:11"
    row(live, "routing/bgp/connection", name="ul-pve2")["use-bfd"] = "yes"
    assert plan_core(NETWORK, CORE_A, live).empty


def test_dynamic_rows_are_invisible() -> None:
    live = core_live()
    live["ipv6/address"].append({".id": "*99", "address": "fe80::1/64", "interface": "vlan101", "dynamic": "true"})
    assert plan_core(NETWORK, CORE_A, live).empty


def test_the_order_comes_from_nextid_not_the_listing() -> None:
    rows = [{".id": "*3", ".nextid": "*ffffffff"}, {".id": "*1", ".nextid": "*2"}, {".id": "*2", ".nextid": "*3"}]
    assert [r[".id"] for r in table_order(rows)] == ["*1", "*2", "*3"]


# --- Drift ---------------------------------------------------------------------------------------------------------


def test_a_changed_field_is_set_back() -> None:
    live = core_live()
    row(live, "routing/bgp/connection", name="ul-pve1")["remote.as"] = "4200000099"

    draft = plan_core(NETWORK, CORE_A, live)

    assert [op.describe() for op in draft.ops] == [
        "~ /routing/bgp/connection name=ul-pve1: remote.as: 4200000099 -> 4200000011"
    ]
    assert [op.describe() for op in draft.inverse_ops] == [
        "~ /routing/bgp/connection name=ul-pve1: remote.as: 4200000011 -> 4200000099"
    ]
    assert_converges(draft, NETWORK, live)


def test_a_managed_field_set_by_hand_goes_back_to_its_default() -> None:
    live = leaf_live()
    row(live, "interface/bonding", name="bond-pve1")["transmit-hash-policy"] = "layer-3-and-4"
    row(live, "interface/bridge/port", interface="peer-link")["disabled"] = "true"

    draft = plan_leaf(NETWORK, LEAF_A, live)

    assert [op.describe() for op in draft.ops] == [
        "~ /interface/bonding name=bond-pve1: transmit-hash-policy: layer-3-and-4 -> (default)",
        "~ /interface/bridge/port bridge=bridge interface=peer-link: disabled: true -> (default)",
    ]
    assert_converges(draft, NETWORK, live, leaf=True)


def test_a_new_node_adds_its_chain_before_its_connection() -> None:
    bigger = Network(fabric=FABRIC, cores=CORES, leafs=LEAFS, nodes=(*NODES, node(4)))

    draft = plan_core(bigger, bigger.core_by_name["core-a"], core_live())

    assert (GOLDEN / "core-a-add-pve4.txt").read_text() == draft.diff
    assert_converges(draft, bigger, core_live())


def test_a_removed_node_loses_its_connection_then_its_chain() -> None:
    smaller = without_pve3()

    draft = plan_core(smaller, smaller.core_by_name["core-a"], core_live())

    assert (GOLDEN / "core-a-remove-pve3.txt").read_text() == draft.diff
    assert all(op.action == "remove" for op in draft.ops)
    assert_converges(draft, smaller, core_live())


def test_a_hand_made_row_is_unmanaged_and_left_alone() -> None:
    live = core_live()
    live["routing/bgp/connection"].append({".id": "*90", "name": "isp1", "remote.as": "64500"})
    live["routing/filter/rule"].insert(0, {".id": "*91", "chain": "isp-in", "rule": "accept"})
    link(live)

    draft = plan_core(NETWORK, CORE_A, live)

    assert draft.empty
    assert [(u.path, u.key) for u in draft.unmanaged] == [
        ("routing/filter/rule", {"chain": "isp-in", "comment": ""}),
        ("routing/bgp/connection", {"name": "isp1"}),
    ]


def test_a_hand_bootstrapped_row_is_adopted_without_a_guard_or_warning() -> None:
    live = core_live()
    for menu in ("interface/vlan", "ipv6/address", "routing/bgp/instance"):
        for r in live[menu]:
            del r["comment"]

    draft = plan_core(NETWORK, CORE_A, live)

    assert {op.action for op in draft.ops} == {"set"}
    assert all(op.fields == {"comment": "sharpwire"} for op in draft.ops)
    assert draft.guard_violations == () and draft.warnings == ()
    assert_converges(draft, NETWORK, live)


def test_a_duplicate_of_an_owned_rule_is_removed() -> None:
    live = core_live()
    reject = row(live, "routing/filter/rule", comment="sharpwire:ul-out:reject")
    live["routing/filter/rule"].append({**reject, ".id": "*92"})
    link(live)

    draft = plan_core(NETWORK, CORE_A, live)

    assert [op.describe() for op in draft.ops] == [
        "- /routing/filter/rule chain=ul-out comment=sharpwire:ul-out:reject"
    ]


def test_rules_out_of_order_are_moved_back() -> None:
    live = core_live()
    rules = live["routing/filter/rule"]
    chain = [r for r in rules if r["chain"] == "pub-in"]
    others = [r for r in rules if r["chain"] != "pub-in"]
    live["routing/filter/rule"] = [*others, *reversed(chain)]
    link(live)

    draft = plan_core(NETWORK, CORE_A, live)

    assert {op.action for op in draft.ops} == {"move"}
    assert len(draft.ops) == 2  # three rules reversed: one stays, two move
    assert_converges(draft, NETWORK, live)


def test_a_missing_physical_port_is_a_problem_not_an_op() -> None:
    live = core_live()
    live["interface/ethernet"] = live["interface/ethernet"][:1]
    with pytest.raises(LiveStateError) as exc:
        plan_core(NETWORK, CORE_A, live)
    assert exc.value.problems == ["The device has no /interface/ethernet name=sfp-sfpplus2."]


def test_ops_create_dependencies_first_and_remove_dependents_first() -> None:
    # Everything on an empty device but its ports: every add, in menu order.
    live = {"interface/ethernet": core_live()["interface/ethernet"]}
    draft = plan_core(NETWORK, CORE_A, live)
    order = list(MENUS)
    paths = [op.path for op in draft.ops]
    assert paths == sorted(paths, key=order.index)
    # And its inverse removes them all, dependents first.
    inverse_paths = [op.path for op in draft.inverse_ops]
    assert inverse_paths == sorted(inverse_paths, key=order.index, reverse=True)


# --- Guards and warnings ------------------------------------------------------------------------------------------


def with_core_a(**update: object) -> Network:
    cores = tuple(c.model_copy(update=update) if c.name == "core-a" else c for c in CORES)
    return Network(fabric=FABRIC, cores=cores, leafs=LEAFS, nodes=NODES)


def test_changing_the_mgmt_address_violates_the_hard_guard() -> None:
    live = core_live()
    row(live, "ipv6/address", interface="vlan10")["address"] = "2001:db8:100::99/64"

    draft = plan_core(NETWORK, CORE_A, live)

    assert draft.guard_violations == (
        "core-a: the plan changes an address on the mgmt interface vlan10 (2001:db8:100::2/64), which the controller's "
        "management session depends on. Make NetBox match the device instead.",
        "core-a: the plan changes an address on the mgmt interface vlan10 (2001:db8:100::99/64), which the controller's "
        "management session depends on. Make NetBox match the device instead.",
    )


def test_a_mgmt_mtu_change_is_a_warning() -> None:
    live = core_live()
    row(live, "interface/vlan", name="vlan10")["mtu"] = "1400"

    draft = plan_core(NETWORK, CORE_A, live)

    assert draft.guard_violations == ()
    assert [w.code for w in draft.warnings] == ["mgmt_interface"]


def test_moving_the_mgmt_interface_violates_the_hard_guard() -> None:
    live = core_live()
    row(live, "interface/vlan", name="vlan10")["vlan-id"] = "11"
    assert plan_core(NETWORK, CORE_A, live).guard_violations


def test_a_bgp_instance_change_is_a_warning() -> None:
    network = with_core_a(asn=4200000002)
    draft = plan_core(network, network.core_by_name["core-a"], core_live())
    assert [w.code for w in draft.warnings] == ["core_bgp_instance"]


def test_leaf_mlag_and_peer_link_vlan_changes_are_warnings() -> None:
    live = leaf_live()
    row(live, "interface/bridge/mlag", bridge="bridge")["peer-port"] = "sfp-sfpplus15"
    vlan = row(live, "interface/bridge/vlan", vlan_ids="300")
    vlan["tagged"] = vlan["tagged"].replace("peer-link,", "")

    draft = plan_leaf(NETWORK, LEAF_A, live)

    assert [w.code for w in draft.warnings] == ["leaf_mlag", "leaf_peer_link_vlans"]
    assert draft.warnings[1].message == "leaf-a: the peer-link's VLANs change (300)."
    assert_converges(draft, NETWORK, live, leaf=True)


# --- Fingerprint, scope and the field table ----------------------------------------------------------------------------


def test_the_fingerprint_ignores_fields_sharpwire_doesnt_manage() -> None:
    live = core_live()
    before = fingerprint(live, CORE_MENUS)
    row(live, "interface/vlan", name="vlan101")["running"] = "true"
    assert fingerprint(live, CORE_MENUS) == before
    row(live, "interface/vlan", name="vlan101")["mtu"] = "1500"
    assert fingerprint(live, CORE_MENUS) != before


def test_the_fingerprint_counts_order_only_within_a_chain() -> None:
    live = core_live()
    before = fingerprint(live, CORE_MENUS)
    live["routing/bgp/connection"].reverse()
    rules = live["routing/filter/rule"]
    live["routing/filter/rule"] = [
        *(r for r in rules if r["chain"] == "pub-in"),
        *(r for r in rules if r["chain"] != "pub-in"),
    ]
    link(live)
    assert fingerprint(live, CORE_MENUS) == before

    pub_in = [r for r in live["routing/filter/rule"] if r["chain"] == "pub-in"]
    live["routing/filter/rule"] = [*reversed(pub_in), *live["routing/filter/rule"][len(pub_in) :]]
    link(live)
    assert fingerprint(live, CORE_MENUS) != before


def test_removing_the_interface_mgmt_sits_on_violates_the_hard_guard() -> None:
    live = core_live()
    live["interface/bonding"].append({".id": "*93", "name": "bond-old", "slaves": "ether9", "comment": "sharpwire"})
    vlan10 = row(live, "interface/vlan", name="vlan10")
    vlan10["interface"] = "bond-old"
    link(live)

    draft = plan_core(NETWORK, CORE_A, live)

    # Moving vlan10 back to bond-leafs, and removing bond-old from under it, both touch the session.
    assert [v.split(",")[0] for v in draft.guard_violations] == [
        "core-a: the plan changes the mgmt interface vlan10",
        "core-a: the plan changes bond-old",
    ]


def test_menus_outside_the_role_are_never_read() -> None:
    live = core_live()
    live["interface/bridge"] = [{".id": "*1", "name": "stray", "comment": "sharpwire"}]
    assert plan_core(NETWORK, CORE_A, live).empty


def test_every_rendered_menu_and_field_is_in_the_tables() -> None:
    for resources, menus in (
        (render_core(NETWORK, CORE_A), CORE_MENUS),
        (render_leaf(NETWORK, LEAF_A), LEAF_MENUS),
    ):
        for r in resources:
            assert r.path in menus
            assert {name for name, _ in r.fields} <= set(FIELDS[r.path])
    assert set(MENUS) == set(FIELDS)


@pytest.mark.parametrize(
    ("field", "a", "b"),
    [
        (Field("duration"), "300ms", "0.3s"),
        (Field("duration"), "1m30s", "00:01:30"),
        (Field("duration"), "90", "1m30s"),
        (Field("bool"), "true", "yes"),
        (Field("list"), "b,a", "a,b"),
        (Field("ip"), "2001:db8::1/64", "2001:0db8:0::1/64"),
        (Field("int"), "0100", "100"),
    ],
)
def test_normalised_values_compare_equal(field: Field, a: str, b: str) -> None:
    assert normalise(field, a) == normalise(field, b)


def test_an_unparseable_value_compares_as_written() -> None:
    assert normalise(Field("ip"), "not-an-ip") == "not-an-ip"
    assert normalise(Field("duration"), "soon") == "soon"


def test_the_plan_doesnt_change_the_live_state_it_reads() -> None:
    live = core_live()
    snapshot = copy.deepcopy(live)
    plan_core(without_pve3(), CORE_A, live)
    assert live == snapshot
