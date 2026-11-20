from ipaddress import IPv4Address, IPv4Interface, IPv6Interface, IPv6Network

import pytest
from pydantic import ValidationError

from app.intent.models import Leaf, Network, Node, PublicRange, VlanInterface
from tests.fixtures.example_network import CORES, FABRIC, LEAFS, NODES, UL_A, UL_B, example_network, node


def _network(**overrides: object) -> Network:
    parts: dict[str, object] = {"fabric": FABRIC, "cores": CORES, "leafs": LEAFS, "nodes": NODES}
    parts.update(overrides)
    return Network.model_validate(parts)


def _pve4(**overrides: object) -> Node:
    return node(4).model_copy(update=overrides)


def _bond_vlans(leaf: Leaf, bond_name: str, vlans: tuple[int, ...]) -> Leaf:
    bonds = tuple(b.model_copy(update={"tagged_vlans": vlans}) if b.name == bond_name else b for b in leaf.bonds)
    return leaf.model_copy(update={"bonds": bonds})


def test_example_network_is_valid() -> None:
    network = example_network()
    assert [n.name for n in network.nodes] == ["pve1", "pve2", "pve3"]


def test_core_underlay_vlan_comes_from_its_interfaces() -> None:
    network = example_network()
    assert network.core_by_name["core-a"].underlay_interface(network.fabric).vlan_id == UL_A
    assert network.core_by_name["core-b"].underlay_interface(network.fabric).vlan_id == UL_B


def test_ipv4_and_ipv6_addresses_are_independent() -> None:
    core_a = CORES[0]
    pub = VlanInterface(
        name="vlan300",
        vlan_id=300,
        mtu=9216,
        addresses=(IPv6Interface("2001:db8:250::abcd/64"), IPv4Interface("10.250.0.200/24")),
    )
    _network(cores=(core_a.model_copy(update={"vlan_interfaces": (*core_a.vlan_interfaces[:2], pub)}), CORES[1]))


def test_duplicate_node_asn_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Every node needs its own underlay ASN"):
        _network(nodes=(*NODES, _pve4(asn=NODES[0].asn)))


def test_node_asn_equal_to_handoff_asn_is_rejected() -> None:
    with pytest.raises(ValidationError, match="belongs to a core, the handoff or the overlay"):
        _network(nodes=(*NODES, _pve4(asn=FABRIC.handoff.asn)))


def test_duplicate_router_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"Router-id 10\.255\.0\.11 is used by 2 devices"):
        _network(nodes=(*NODES, _pve4(router_id=IPv4Address("10.255.0.11"))))


def test_loopback_outside_vtep_prefix_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be a /128 inside 2001:db8:ffff::/64"):
        _network(nodes=(*NODES, _pve4(loopback=IPv6Interface("2001:db8:fffe::14/128"))))


def test_address_outside_its_vlan_prefix_is_rejected() -> None:
    stray = VlanInterface(name="bond0.101", vlan_id=UL_A, addresses=(IPv6Interface("2001:db8:999::14/64"),))
    pve4 = node(4)
    with pytest.raises(ValidationError, match="isn't inside 2001:db8:101::/64"):
        _network(nodes=(*NODES, _pve4(vlan_interfaces=(stray, pve4.vlan_interfaces[1]))))


def test_address_used_twice_is_rejected() -> None:
    clash = (NODES[0].vlan_interfaces[0], node(4).vlan_interfaces[1])
    with pytest.raises(ValidationError, match="is used more than once"):
        _network(nodes=(*NODES, _pve4(vlan_interfaces=clash)))


def test_node_without_an_underlay_address_is_rejected() -> None:
    with pytest.raises(ValidationError, match="has no address on underlay VLAN 102"):
        _network(nodes=(*NODES, _pve4(vlan_interfaces=node(4).vlan_interfaces[:1])))


def test_mlag_id_mismatch_between_leafs_is_rejected() -> None:
    leaf_a, leaf_b = LEAFS
    bonds = tuple(b.model_copy(update={"mlag_id": 99}) if b.name == "bond-pve1" else b for b in leaf_b.bonds)
    with pytest.raises(ValidationError, match="same mlag-id"):
        _network(leafs=(leaf_a, leaf_b.model_copy(update={"bonds": bonds})))


def test_node_bond_missing_a_vlan_is_rejected() -> None:
    leafs = tuple(_bond_vlans(leaf, "bond-pve1", (10, 101, 300)) for leaf in LEAFS)
    with pytest.raises(ValidationError, match=r"bond-pve1 must carry VLANs \[10, 101, 102, 300\]"):
        _network(leafs=leafs)


def test_core_bond_carrying_the_other_underlay_is_rejected() -> None:
    leafs = tuple(_bond_vlans(leaf, "bond-coreA", (10, 101, 102, 300)) for leaf in LEAFS)
    with pytest.raises(ValidationError, match=r"bond-coreA must carry VLANs \[10, 101, 300\]"):
        _network(leafs=leafs)


def test_peer_link_must_carry_every_bond_vlan() -> None:
    leaf_a, leaf_b = LEAFS
    short = leaf_a.peer_link.model_copy(update={"tagged_vlans": (10, 101, 300)})
    with pytest.raises(ValidationError, match=r"must carry every VLAN its bonds carry; missing \[102\]"):
        _network(leafs=(leaf_a.model_copy(update={"peer_link": short}), leaf_b))


def test_port_l2mtu_must_fit_a_tagged_frame() -> None:
    leaf_a, leaf_b = LEAFS
    small = leaf_a.peer_link.members[0].model_copy(update={"l2mtu": 9216})
    peer_link = leaf_a.peer_link.model_copy(update={"members": (small, leaf_a.peer_link.members[1])})
    with pytest.raises(ValidationError, match="needs l2mtu of at least 9220"):
        _network(leafs=(leaf_a.model_copy(update={"peer_link": peer_link}), leaf_b))


def test_public_range_max_length_must_fit() -> None:
    with pytest.raises(ValidationError, match="doesn't fit"):
        PublicRange(prefix=IPv6Network("2001:db8:70::/48"), max_len=40)
