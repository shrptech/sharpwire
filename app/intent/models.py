"""The vendor-neutral intent model for the phase-1 network.

Every value here comes from NetBox: the intent builder reads it (later), and tests build it from fixtures shaped
like NetBox data. docs/02-intent-model.md maps each field to its NetBox object. Nothing in this module invents
network values; the only rules here are validation rules (docs/03-pipeline-and-validation.md).

Addresses are NetBox IP addresses with their mask, assigned to interfaces. An interface's IPv4 and IPv6 addresses
are independent, and nothing is derived from a shared host number.
"""

from collections import Counter
from functools import cached_property
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Address, IPv6Interface, IPv6Network
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Fabric-wide intent: VLANs, prefixes and ASNs, found in NetBox by role or tag ---------------------------------


class UnderlayVlan(_Frozen):
    """A shared underlay VLAN (NetBox VLAN with role `underlay`) and its prefix."""

    vlan_id: int
    prefix: IPv6Network


class PublicRange(_Frozen):
    """A public aggregate (NetBox prefix with role `public`). Subnets from its length up to `max_len` are accepted."""

    prefix: IPv4Network | IPv6Network
    max_len: int

    @model_validator(mode="after")
    def _length_fits(self) -> Self:
        if not self.prefix.prefixlen <= self.max_len <= self.prefix.max_prefixlen:
            raise ValueError(f"Maximum length {self.max_len} doesn't fit {self.prefix}.")
        return self


class PublicHandoff(_Frozen):
    """VLAN 300 (role `public-handoff`): every node's vrf_public peers with both cores, with one shared ASN."""

    vlan_id: int
    prefix4: IPv4Network
    prefix6: IPv6Network
    asn: int
    public_ranges: tuple[PublicRange, ...]


class Fabric(_Frozen):
    overlay_asn: int
    mgmt_vlan_id: int
    vtep_prefix: IPv6Network
    underlay: tuple[UnderlayVlan, ...]
    handoff: PublicHandoff

    @cached_property
    def underlay_ids(self) -> frozenset[int]:
        return frozenset(u.vlan_id for u in self.underlay)


# --- Device interfaces, as NetBox models them --------------------------------------------------------------------


class Port(_Frozen):
    """A physical interface: NetBox `mtu`, plus the `l2mtu` custom field."""

    name: str
    mtu: int
    l2mtu: int


class Lag(_Frozen):
    """A NetBox LAG interface with its member ports.

    `peer` is the device at the other end of the members' cables. `mlag_id` is a custom field, set on MLAG bonds
    only. `tagged_vlans` are the LAG's 802.1Q tagged VLANs.
    """

    name: str
    members: tuple[Port, ...]
    mtu: int
    peer: str | None = None
    mlag_id: int | None = None
    tagged_vlans: tuple[int, ...] = ()


class VlanInterface(_Frozen):
    """A NetBox virtual interface on a VLAN, with its assigned addresses."""

    name: str
    vlan_id: int
    mtu: int | None = None
    addresses: tuple[IPv4Interface | IPv6Interface, ...] = ()

    def address(self, version: int) -> IPv4Interface | IPv6Interface | None:
        return next((a for a in self.addresses if a.version == version), None)


# --- Devices ----------------------------------------------------------------------------------------------------


class Node(_Frozen):
    """A Proxmox node, as the routers see it. ASN and router-id are device custom fields."""

    name: str
    asn: int
    router_id: IPv4Address
    loopback: IPv6Interface
    vlan_interfaces: tuple[VlanInterface, ...]

    def address_on(self, vlan_id: int) -> IPv6Address | None:
        iface = next((v for v in self.vlan_interfaces if v.vlan_id == vlan_id), None)
        address = iface.address(6) if iface else None
        return address.ip if isinstance(address, IPv6Interface) else None


class Core(_Frozen):
    """A CCR2116 core. Its underlay VLAN is the underlay VLAN it has an interface on."""

    name: str
    asn: int
    router_id: IPv4Address
    lag: Lag
    vlan_interfaces: tuple[VlanInterface, ...]

    def vlan_interface(self, vlan_id: int) -> VlanInterface:
        return next(v for v in self.vlan_interfaces if v.vlan_id == vlan_id)

    def underlay_interface(self, fabric: Fabric) -> VlanInterface:
        return next(v for v in self.vlan_interfaces if v.vlan_id in fabric.underlay_ids)


class Leaf(_Frozen):
    """A CRS317 leaf in the MLAG pair: L2 only, apart from its mgmt interface on the bridge."""

    name: str
    bridge: str
    peer_link: Lag
    bonds: tuple[Lag, ...]
    mgmt: VlanInterface


class Network(_Frozen):
    """The whole phase-1 network. Cross-target rules live here (docs/03-pipeline-and-validation.md)."""

    fabric: Fabric
    cores: tuple[Core, ...]
    leafs: tuple[Leaf, ...]
    nodes: tuple[Node, ...]

    @cached_property
    def node_by_name(self) -> dict[str, Node]:
        return {n.name: n for n in self.nodes}

    @cached_property
    def core_by_name(self) -> dict[str, Core]:
        return {c.name: c for c in self.cores}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        errors = [
            *self._asn_and_router_id_errors(),
            *self._underlay_errors(),
            *self._address_errors(),
            *self._leaf_errors(),
        ]
        if errors:
            raise ValueError(" ".join(errors))
        return self

    def _asn_and_router_id_errors(self) -> list[str]:
        f = self.fabric
        errors: list[str] = []
        for asn, count in Counter(n.asn for n in self.nodes).items():
            if count > 1:
                errors.append(f"Node ASN {asn} is used by {count} nodes. Every node needs its own underlay ASN.")
        reserved = {f.overlay_asn, f.handoff.asn, *(c.asn for c in self.cores)}
        for n in self.nodes:
            if n.asn in reserved:
                errors.append(f"Node {n.name} uses ASN {n.asn}, which belongs to a core, the handoff or the overlay.")
        devices: list[Node | Core] = [*self.nodes, *self.cores]
        for rid, count in Counter(d.router_id for d in devices).items():
            if count > 1:
                errors.append(f"Router-id {rid} is used by {count} devices.")
        return errors

    def _underlay_errors(self) -> list[str]:
        f = self.fabric
        errors: list[str] = []
        for u in f.underlay:
            on_vlan = [c.name for c in self.cores if any(v.vlan_id == u.vlan_id for v in c.vlan_interfaces)]
            if len(on_vlan) != 1:
                errors.append(f"Underlay VLAN {u.vlan_id} needs exactly one core; it has {len(on_vlan) or 'none'}.")
            for n in self.nodes:
                if n.address_on(u.vlan_id) is None:
                    errors.append(f"Node {n.name} has no address on underlay VLAN {u.vlan_id}.")
        for c in self.cores:
            if sum(v.vlan_id in f.underlay_ids for v in c.vlan_interfaces) != 1:
                errors.append(f"Core {c.name} must be on exactly one underlay VLAN.")
        for n in self.nodes:
            if n.loopback.network.prefixlen != 128 or n.loopback.ip not in f.vtep_prefix:
                errors.append(f"Node {n.name}'s loopback {n.loopback} must be a /128 inside {f.vtep_prefix}.")
        return errors

    def _address_errors(self) -> list[str]:
        """Every address lies inside its VLAN's prefix, and no address is used twice in a VLAN."""
        f = self.fabric
        prefixes: dict[int, list[IPv4Network | IPv6Network]] = {
            **{u.vlan_id: [u.prefix] for u in f.underlay},
            f.handoff.vlan_id: [f.handoff.prefix4, f.handoff.prefix6],
        }
        errors: list[str] = []
        seen: dict[IPv4Address | IPv6Address, list[str]] = {}
        owners: list[tuple[str, VlanInterface]] = [
            *((c.name, v) for c in self.cores for v in c.vlan_interfaces),
            *((n.name, v) for n in self.nodes for v in n.vlan_interfaces),
            *((leaf.name, leaf.mgmt) for leaf in self.leafs),
        ]
        for owner, iface in owners:
            for address in iface.addresses:
                seen.setdefault(address.ip, []).append(owner)
                allowed = [p for p in prefixes.get(iface.vlan_id, []) if p.version == address.version]
                if allowed and not any(address.network == p for p in allowed):
                    errors.append(f"{owner} has {address} on VLAN {iface.vlan_id}, which isn't inside {allowed[0]}.")
        for ip, names in seen.items():
            if len(names) > 1:
                errors.append(f"Address {ip} is used more than once ({', '.join(names)}).")
        return errors

    def _leaf_errors(self) -> list[str]:
        """MLAG pair consistency and the VLAN membership rule (docs/03-pipeline-and-validation.md)."""
        f = self.fabric
        errors: list[str] = []
        if len(self.leafs) == 2:
            a, b = ({(bd.name, bd.peer, bd.mlag_id, bd.tagged_vlans) for bd in leaf.bonds} for leaf in self.leafs)
            if a != b:
                errors.append("The two leafs must have the same bonds, with the same mlag-id, peer and VLANs on each.")
        node_vlans = {f.mgmt_vlan_id, *f.underlay_ids, f.handoff.vlan_id}
        for leaf in self.leafs:
            carried: set[int] = set()
            for bd in leaf.bonds:
                carried |= set(bd.tagged_vlans)
                if bd.mlag_id is None:
                    errors.append(f"Leaf {leaf.name} bond {bd.name} has no mlag-id.")
                core = self.core_by_name.get(bd.peer or "")
                if core is not None:
                    expected = {f.mgmt_vlan_id, core.underlay_interface(f).vlan_id, f.handoff.vlan_id}
                elif bd.peer in self.node_by_name:
                    expected = node_vlans
                else:
                    errors.append(f"Leaf {leaf.name} bond {bd.name} points at unknown device {bd.peer}.")
                    continue
                if set(bd.tagged_vlans) != expected:
                    errors.append(
                        f"Leaf {leaf.name} bond {bd.name} must carry VLANs {sorted(expected)}, "
                        f"not {sorted(bd.tagged_vlans)}."
                    )
            missing = sorted(carried - set(leaf.peer_link.tagged_vlans))
            if missing:
                errors.append(f"Leaf {leaf.name}'s peer-link must carry every VLAN its bonds carry; missing {missing}.")
            if leaf.peer_link.mlag_id is not None:
                errors.append(f"Leaf {leaf.name}'s peer-link must not be an MLAG bond.")
            ports = [p.name for lag in (leaf.peer_link, *leaf.bonds) for p in lag.members]
            for port, count in Counter(ports).items():
                if count > 1:
                    errors.append(f"Leaf {leaf.name} uses port {port} more than once.")
            for lag in (leaf.peer_link, *leaf.bonds):
                for p in lag.members:
                    if p.l2mtu < lag.mtu + 4:
                        errors.append(f"Leaf {leaf.name} port {p.name} needs l2mtu of at least {lag.mtu + 4}.")
        return errors
