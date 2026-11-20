"""The network doc's example network (docs/network/proxmox-evpn-ipv6-underlay.md), phase 1: three nodes.

Shaped like NetBox data: interfaces with their MTUs, LAG members, tagged VLANs, and IP addresses with masks.
Values come from the doc's addressing plan and Appendix A. The cores' own mgmt addresses and router-ids for the
nodes aren't all in the doc; this fixture picks ::2/::3 for the cores' mgmt and 10.255.0.1x for the nodes.

When the intent builder exists (build step 2), this fixture is replaced by a NetBox GraphQL snapshot fed
through the builder.
"""

from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Interface, IPv6Network

from app.intent.models import (
    Core,
    Fabric,
    Lag,
    Leaf,
    Network,
    Node,
    Port,
    PublicHandoff,
    PublicRange,
    UnderlayVlan,
    VlanInterface,
)

MGMT, UL_A, UL_B, PUB = 10, 101, 102, 300
FABRIC_VLANS = (MGMT, UL_A, UL_B, PUB)


def v6(address: str) -> IPv6Interface:
    return IPv6Interface(address)


def sfp(n: int) -> Port:
    return Port(name=f"sfp-sfpplus{n}", mtu=9216, l2mtu=9500)


FABRIC = Fabric(
    overlay_asn=65000,
    mgmt_vlan_id=MGMT,
    vtep_prefix=IPv6Network("2001:db8:ffff::/64"),
    underlay=(
        UnderlayVlan(vlan_id=UL_A, prefix=IPv6Network("2001:db8:101::/64")),
        UnderlayVlan(vlan_id=UL_B, prefix=IPv6Network("2001:db8:102::/64")),
    ),
    handoff=PublicHandoff(
        vlan_id=PUB,
        prefix4=IPv4Network("10.250.0.0/24"),
        prefix6=IPv6Network("2001:db8:250::/64"),
        asn=4200000100,
        public_ranges=(
            PublicRange(prefix=IPv4Network("198.51.100.0/24"), max_len=28),
            PublicRange(prefix=IPv6Network("2001:db8:70::/48"), max_len=64),
        ),
    ),
)


def node(i: int) -> Node:
    return Node(
        name=f"pve{i}",
        asn=4200000010 + i,
        router_id=IPv4Address(f"10.255.0.{10 + i}"),
        loopback=v6(f"2001:db8:ffff::{10 + i}/128"),
        vlan_interfaces=(
            VlanInterface(name=f"bond0.{UL_A}", vlan_id=UL_A, addresses=(v6(f"2001:db8:101::{10 + i}/64"),)),
            VlanInterface(name=f"bond0.{UL_B}", vlan_id=UL_B, addresses=(v6(f"2001:db8:102::{10 + i}/64"),)),
        ),
    )


NODES = tuple(node(i) for i in (1, 2, 3))


def core(name: str, asn: int, router_id: str, mgmt: str, underlay: tuple[int, str], pub4: str, pub6: str) -> Core:
    ul_vlan, ul_address = underlay
    return Core(
        name=name,
        asn=asn,
        router_id=IPv4Address(router_id),
        lag=Lag(name="bond-leafs", members=(sfp(1), sfp(2)), mtu=9216),
        vlan_interfaces=(
            VlanInterface(name=f"vlan{MGMT}", vlan_id=MGMT, mtu=1500, addresses=(v6(mgmt),)),
            VlanInterface(name=f"vlan{ul_vlan}", vlan_id=ul_vlan, mtu=9216, addresses=(v6(ul_address),)),
            VlanInterface(name=f"vlan{PUB}", vlan_id=PUB, mtu=9216, addresses=(v6(pub6), IPv4Interface(pub4))),
        ),
    )


CORES = (
    core(
        "core-a", 4200000001, "10.255.255.1", "2001:db8:100::2/64", (UL_A, "2001:db8:101::1/64"),
        "10.250.0.1/24", "2001:db8:250::1/64",
    ),
    core(
        "core-b", 4200000002, "10.255.255.2", "2001:db8:100::3/64", (UL_B, "2001:db8:102::1/64"),
        "10.250.0.2/24", "2001:db8:250::2/64",
    ),
)  # fmt: skip


def leaf_bonds() -> tuple[Lag, ...]:
    def bond(name: str, peer: str, port: int, mlag_id: int, vlans: tuple[int, ...]) -> Lag:
        return Lag(name=name, members=(sfp(port),), mtu=9216, peer=peer, mlag_id=mlag_id, tagged_vlans=vlans)

    return (
        *(bond(f"bond-pve{i}", f"pve{i}", i, 10 + i, FABRIC_VLANS) for i in (1, 2, 3)),
        bond("bond-coreA", "core-a", 13, 21, (MGMT, UL_A, PUB)),
        bond("bond-coreB", "core-b", 14, 22, (MGMT, UL_B, PUB)),
    )


LEAFS = tuple(
    Leaf(
        name=f"leaf-{x}",
        bridge="bridge",
        peer_link=Lag(name="peer-link", members=(sfp(15), sfp(16)), mtu=9216, tagged_vlans=FABRIC_VLANS),
        bonds=leaf_bonds(),
        mgmt=VlanInterface(name="mgmt", vlan_id=MGMT, addresses=(v6(f"2001:db8:100::{suffix}/64"),)),
    )
    for x, suffix in (("a", "f1"), ("b", "f2"))
)


def example_network() -> Network:
    return Network(fabric=FABRIC, cores=CORES, leafs=LEAFS, nodes=NODES)
