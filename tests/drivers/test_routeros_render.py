from pathlib import Path

import pytest

from app.drivers.routeros.render import render_core, render_leaf
from app.drivers.routeros.resources import Resource, item, ordered, to_rsc
from app.intent.models import Network
from tests.fixtures.example_network import CORES, FABRIC, LEAFS, NODES, example_network, node

GOLDEN = Path(__file__).parent.parent / "golden" / "routeros"


@pytest.mark.parametrize("name", ["core-a", "core-b"])
def test_core_matches_golden(name: str) -> None:
    network = example_network()
    rendered = to_rsc(render_core(network, network.core_by_name[name]))
    assert rendered == (GOLDEN / f"{name}.rsc").read_text()


def test_leaf_a_matches_golden() -> None:
    network = example_network()
    rendered = to_rsc(render_leaf(network, network.leafs[0]))
    assert rendered == (GOLDEN / "leaf-a.rsc").read_text()


def test_leaf_b_differs_only_in_mgmt_address() -> None:
    # Network doc A.1: "Leaf B is identical apart from its mgmt address (::f2)."
    network = example_network()
    a = to_rsc(render_leaf(network, network.leafs[0]))
    b = to_rsc(render_leaf(network, network.leafs[1]))
    assert b == a.replace("2001:db8:100::f1/64", "2001:db8:100::f2/64")


@pytest.mark.parametrize("device", ["core-a", "core-b", "leaf-a", "leaf-b"])
def test_identities_are_unique(device: str) -> None:
    network = example_network()
    resources = (
        render_core(network, network.core_by_name[device])
        if device.startswith("core")
        else render_leaf(network, next(leaf for leaf in network.leafs if leaf.name == device))
    )
    identities = [r.identity for r in resources]
    assert len(identities) == len(set(identities))


def test_every_referenced_filter_chain_exists() -> None:
    network = example_network()
    resources = render_core(network, network.core_by_name["core-a"])
    chains = {dict(r.fields)["chain"] for r in resources if r.path == "routing/filter/rule"}
    for r in resources:
        if r.path == "routing/bgp/connection":
            fields = dict(r.fields)
            assert fields["input.filter"] in chains
            assert fields["output.filter-chain"] in chains


def test_adding_a_node_adds_only_its_chain_and_connection() -> None:
    network = example_network()
    before = {r.identity for r in render_core(network, network.core_by_name["core-a"])}
    bigger = Network(fabric=FABRIC, cores=CORES, leafs=LEAFS, nodes=(*NODES, node(4)))
    after = {r.identity for r in render_core(bigger, bigger.core_by_name["core-a"])}
    added = after - before
    assert before <= after
    assert added == {
        ("routing/filter/rule", (("chain", "ul-in-pve4"), ("comment", "sharpwire:ul-in-pve4:accept-loopback"))),
        ("routing/filter/rule", (("chain", "ul-in-pve4"), ("comment", "sharpwire:ul-in-pve4:reject"))),
        ("routing/bgp/connection", (("name", "ul-pve4"),)),
    }


def test_leaf_vlans_follow_netbox_tagged_vlans() -> None:
    # A VLAN added to a bond's tagged VLANs in NetBox shows up in the bridge VLAN table, with no code change.
    network = example_network()
    leaf = network.leafs[0]
    bonds = tuple(b.model_copy(update={"tagged_vlans": (*b.tagged_vlans, 999)}) for b in leaf.bonds[:1])
    peer_link = leaf.peer_link.model_copy(update={"tagged_vlans": (*leaf.peer_link.tagged_vlans, 999)})
    changed = leaf.model_copy(update={"bonds": bonds + leaf.bonds[1:], "peer_link": peer_link})
    vlans = {
        dict(r.fields)["vlan-ids"]: dict(r.fields)["tagged"]
        for r in render_leaf(network, changed)
        if r.path == "interface/bridge/vlan"
    }
    assert vlans["999"] == "peer-link,bond-pve1"


def test_rsc_quotes_values_with_spaces_and_dollar() -> None:
    rendered = to_rsc([item("system/script", ("name", "x"), ("source", 'say "$hi"'))])
    assert rendered == '/system script\nadd name=x source="say \\"\\$hi\\""\n'


def test_resource_rejects_missing_key_field() -> None:
    with pytest.raises(ValueError, match="key fields"):
        Resource(path="interface/vlan", fields=(("vlan-id", "10"),))


def test_ordered_rules_are_keyed_by_chain_and_comment_not_position() -> None:
    first = ordered("routing/filter/rule", "ul-out", "reject", 1, ("rule", "reject"))
    moved = ordered("routing/filter/rule", "ul-out", "reject", 5, ("rule", "reject"))
    assert first.identity == ("routing/filter/rule", (("chain", "ul-out"), ("comment", "sharpwire:ul-out:reject")))
    assert first.identity == moved.identity
    assert (first.position, moved.position) == (1, 5)


def test_rule_comments_are_unique_per_device() -> None:
    network = example_network()
    comments = [
        dict(r.fields)["comment"]
        for r in render_core(network, network.core_by_name["core-a"])
        if r.path == "routing/filter/rule"
    ]
    assert len(comments) == len(set(comments))
