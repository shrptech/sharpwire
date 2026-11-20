"""Render the desired RouterOS state for cores and leafs from the intent model.

Values (names, MTUs, VLANs, addresses, ASNs) come from the intent, which comes from NetBox. The fixed shapes below
are the named rules listed in docs/02-intent-model.md ("Rules"); change one there and here together.

v1 scope (docs/04-drivers.md): sharpwire owns exactly the rows rendered here. Upstream ISP sessions, VRRP, the
firewall and system services are hand-configured in v1 and never touched.
"""

from ipaddress import IPv4Interface, IPv6Interface

from app.drivers.routeros.resources import Resource, baseline, item, ordered, settings
from app.intent.models import Core, Lag, Leaf, Network, Port, PublicRange, VlanInterface

# Named rules (docs/02-intent-model.md, "Rules").
LACP = (("mode", "802.3ad"), ("lacp-rate", "1sec"))
CORE_BOND_HASH = ("transmit-hash-policy", "layer-3-and-4")
BGP_INSTANCE = "core"
# BFD: FRR's defaults on the nodes (network doc Step 4c), mirrored on the cores.
BFD = (("min-rx", "300ms"), ("min-tx", "300ms"), ("multiplier", "3"))
EDGE_FRAME_TYPES = ("frame-types", "admit-only-vlan-tagged")
UL_OUT, PUB_IN, DEFAULT_OUT = "ul-out", "pub-in", "default-out"


def underlay_in_chain(node_name: str) -> str:
    return f"ul-in-{node_name}"


def underlay_connection_name(node_name: str) -> str:
    return f"ul-{node_name}"


def _ports(ports: tuple[Port, ...]) -> list[Resource]:
    return [
        baseline("interface/ethernet", ("name", p.name), ("l2mtu", str(p.l2mtu)), ("mtu", str(p.mtu))) for p in ports
    ]


def _bond(lag: Lag, *extra: tuple[str, str]) -> Resource:
    fields: list[tuple[str, str]] = [("name", lag.name), ("slaves", ",".join(p.name for p in lag.members)), *LACP]
    fields += extra
    if lag.mlag_id is not None:
        fields.append(("mlag-id", str(lag.mlag_id)))
    fields.append(("mtu", str(lag.mtu)))
    return item("interface/bonding", *fields)


def _vlan_interface(iface: VlanInterface, parent: str) -> Resource:
    fields = [("name", iface.name), ("interface", parent), ("vlan-id", str(iface.vlan_id))]
    if iface.mtu is not None:
        fields.append(("mtu", str(iface.mtu)))
    return item("interface/vlan", *fields)


def _addresses(ifaces: list[VlanInterface]) -> list[Resource]:
    key = ("address", "interface")
    v6 = [
        item("ipv6/address", ("address", str(a)), ("interface", i.name), ("advertise", "no"), key=key)
        for i in ifaces
        for a in i.addresses
        if isinstance(a, IPv6Interface)
    ]
    v4 = [
        item("ip/address", ("address", str(a)), ("interface", i.name), key=key)
        for i in ifaces
        for a in i.addresses
        if isinstance(a, IPv4Interface)
    ]
    return v6 + v4


def _range_rule(r: PublicRange) -> tuple[str, str]:
    lengths = f"{r.prefix.prefixlen}-{r.max_len}"
    return (f"accept-{r.prefix}", f"if (dst in {r.prefix} && dst-len in {lengths}) {{ accept }}")


def _chain(chain: str, rules: list[tuple[str, str]]) -> list[Resource]:
    """Rules as (name, rule text), in evaluation order. The name becomes part of the rule's comment."""
    return [ordered("routing/filter/rule", chain, name, i, ("rule", rule)) for i, (name, rule) in enumerate(rules)]


REJECT = ("reject", "reject")


def render_core(network: Network, core: Core) -> list[Resource]:
    f = network.fabric
    handoff = f.handoff
    ifaces = sorted(core.vlan_interfaces, key=lambda v: v.vlan_id)
    underlay = core.underlay_interface(f)
    pub = core.vlan_interface(handoff.vlan_id)
    ul_address, pub4, pub6 = underlay.address(6), pub.address(4), pub.address(6)
    assert ul_address is not None and pub4 is not None and pub6 is not None

    out = _ports(core.lag.members)
    out.append(_bond(core.lag, CORE_BOND_HASH))
    out += [_vlan_interface(i, core.lag.name) for i in ifaces]
    out += _addresses(ifaces)

    out.append(
        item("routing/bgp/instance", ("name", BGP_INSTANCE), ("as", str(core.asn)), ("router-id", str(core.router_id)))
    )
    # Rule: BFD covers every interface with BFD-enabled sessions (RouterOS won't bring BGP up without it).
    out.append(
        item("routing/bfd/configuration", ("interfaces", f"{underlay.name},{pub.name}"), *BFD, key=("interfaces",))
    )

    for node in network.nodes:
        accept_own = ("accept-loopback", f"if (dst == {node.loopback}) {{ accept }}")
        out += _chain(underlay_in_chain(node.name), [accept_own, REJECT])
    out += _chain(UL_OUT, [("accept-vteps", f"if (dst in {f.vtep_prefix} && dst-len == 128) {{ accept }}"), REJECT])
    out += _chain(PUB_IN, [*(_range_rule(r) for r in handoff.public_ranges), REJECT])
    out += _chain(DEFAULT_OUT, [("accept-defaults", "if (dst == 0.0.0.0/0 || dst == ::/0) { accept }"), REJECT])

    for node in network.nodes:
        out.append(
            item(
                "routing/bgp/connection",
                ("name", underlay_connection_name(node.name)),
                ("instance", BGP_INSTANCE),
                ("afi", "ipv6"),
                ("local.role", "ebgp"),
                ("local.address", str(ul_address.ip)),
                ("remote.address", str(node.address_on(underlay.vlan_id))),
                ("remote.as", str(node.asn)),
                ("use-bfd", "yes"),
                # propagate, not default: with default, RouterOS 7.25 sends itself as the next hop and node-to-node
                # traffic hairpins through the core (network doc Step 4b; spikes/routeros/FINDINGS.md, Q6).
                ("nexthop-choice", "propagate"),
                ("input.filter", underlay_in_chain(node.name)),
                ("output.filter-chain", UL_OUT),
            )
        )

    # One listening connection per family covers every node: the remote address is the whole handoff prefix.
    for name, afi, local, remote in (
        ("pub4", "ip", pub4.ip, handoff.prefix4),
        ("pub6", "ipv6", pub6.ip, handoff.prefix6),
    ):
        out.append(
            item(
                "routing/bgp/connection",
                ("name", name),
                ("instance", BGP_INSTANCE),
                ("afi", afi),
                ("local.role", "ebgp"),
                ("local.address", str(local)),
                ("remote.address", str(remote)),
                ("remote.as", str(handoff.asn)),
                ("use-bfd", "yes"),
                ("input.filter", PUB_IN),
                ("output.filter-chain", DEFAULT_OUT),
                ("output.default-originate", "always"),
            )
        )
    return out


def render_leaf(network: Network, leaf: Leaf) -> list[Resource]:
    lags = (leaf.peer_link, *leaf.bonds)
    out = _ports(tuple(p for lag in lags for p in lag.members))
    out += [_bond(lag) for lag in lags]

    out.append(item("interface/bridge", ("name", leaf.bridge), ("vlan-filtering", "yes")))
    out.append(settings("interface/bridge/mlag", ("bridge", leaf.bridge), ("peer-port", leaf.peer_link.name)))

    port_key = ("bridge", "interface")
    out.append(item("interface/bridge/port", ("bridge", leaf.bridge), ("interface", leaf.peer_link.name), key=port_key))
    for bond in leaf.bonds:
        # Rule: edge bonds (MLAG bonds to nodes and cores) admit tagged frames only.
        out.append(
            item(
                "interface/bridge/port",
                ("bridge", leaf.bridge),
                ("interface", bond.name),
                EDGE_FRAME_TYPES,
                key=port_key,
            )
        )

    # Bridge VLANs come straight from the LAGs' tagged VLANs in NetBox. The bridge itself is tagged only where the
    # leaf has its own VLAN interface (mgmt).
    vlan_ids = sorted({v for lag in lags for v in lag.tagged_vlans})
    for vlan_id in vlan_ids:
        cpu = [leaf.bridge] if vlan_id == leaf.mgmt.vlan_id else []
        tagged = [*cpu, *(lag.name for lag in lags if vlan_id in lag.tagged_vlans)]
        out.append(
            item(
                "interface/bridge/vlan",
                ("bridge", leaf.bridge),
                ("vlan-ids", str(vlan_id)),
                ("tagged", ",".join(tagged)),
                key=("bridge", "vlan-ids"),
            )
        )

    out.append(_vlan_interface(leaf.mgmt, leaf.bridge))
    out += _addresses([leaf.mgmt])
    return out
