"""The intent builder: a NetBox GraphQL snapshot in, the intent model out (docs/02-intent-model.md#netbox-mapping).

Pure: the snapshot is what `app.services.netbox.fetch_intent_snapshot()` returns, fetched elsewhere or loaded from a
fixture. Every value comes from the snapshot. When NetBox is missing something or holds it in the wrong shape, the
builder says what to fix in NetBox, all problems at once (BuildError), then the model's own cross-target validation
adds its problems.

Collections are sorted (devices and interfaces by natural name, VLANs by ID, addresses IPv6 first), so the same
NetBox data always builds the same model and renders the same config.
"""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Interface,
    IPv6Network,
    ip_interface,
    ip_network,
)
from typing import Any

from pydantic import ValidationError

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

Snapshot = Mapping[str, Any]
Obj = Mapping[str, Any]

# Device role → the platform its driver handles (docs/02-intent-model.md: the driver is chosen by platform).
PLATFORMS = {"core": "routeros", "leaf": "routeros", "pve-node": "proxmox"}

LOOPBACK = "dummy0"


class BuildError(ValueError):
    """NetBox doesn't describe a valid network. `problems` are plain sentences saying what to fix."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(" ".join(problems))
        self.problems = problems


def natural_key(name: str) -> tuple[Any, ...]:
    """`sfp-sfpplus2` before `sfp-sfpplus13`, as NetBox orders interfaces."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


@dataclass
class _Builder:
    snapshot: Snapshot
    problems: list[str] = field(default_factory=list)
    # NetBox VLAN id → VID, for the fabric VLANs only.
    fabric_vlans: dict[str, int] = field(default_factory=dict)

    def fail(self, problem: str) -> None:
        self.problems.append(problem)

    def one[T](self, values: list[T], what: str) -> T | None:
        if len(values) == 1:
            return values[0]
        self.fail(f"Expected one {what}, found {len(values) or 'none'}.")
        return None

    # --- Fabric ---------------------------------------------------------------------------------------------------

    def fabric(self) -> Fabric | None:
        vlans: dict[str, list[int]] = {}
        for vlan in self.snapshot["vlan_list"]:
            vlans.setdefault(vlan["role"]["slug"], []).append(vlan["vid"])
            self.fabric_vlans[str(vlan["id"])] = vlan["vid"]
        for vid, count in Counter(self.fabric_vlans.values()).items():
            if count > 1:
                self.fail(f"{count} fabric VLANs have ID {vid}. Tag only one of them sharpwire-managed.")
        mgmt = self.one(sorted(vlans.get("mgmt", [])), "VLAN with role mgmt")
        handoff_vid = self.one(sorted(vlans.get("public-handoff", [])), "VLAN with role public-handoff")
        underlay_vids = sorted(set(vlans.get("underlay", [])))
        if not underlay_vids:
            self.fail("No VLAN has role underlay. Add one per core.")

        by_vlan: dict[int, list[IPv4Network | IPv6Network]] = {}
        for p in self.snapshot["vlan_prefixes"]:
            if p.get("vlan") is None:
                self.fail(f"Prefix {p['prefix']}'s VLAN isn't visible. Let the NetBox token view VLANs.")
                continue
            on_vid = self.fabric_vlans.get(str(p["vlan"]["id"]))
            if on_vid is None:
                self.fail(f"Prefix {p['prefix']} is on a VLAN that isn't tagged sharpwire-managed.")
                continue
            by_vlan.setdefault(on_vid, []).append(ip_network(p["prefix"]))
        underlay = []
        for vid in underlay_vids:
            v6 = [p for p in by_vlan.get(vid, []) if p.version == 6]
            prefix6 = self.one(v6, f"IPv6 prefix on underlay VLAN {vid}")
            if isinstance(prefix6, IPv6Network):
                underlay.append(UnderlayVlan(vlan_id=vid, prefix=prefix6))

        by_role: dict[str, list[Obj]] = {}
        for p in self.snapshot["role_prefixes"]:
            by_role.setdefault(p["role"]["slug"], []).append(p)
        vtep = self.one([ip_network(p["prefix"]) for p in by_role.get("vtep", [])], "prefix with role vtep")
        if vtep is not None and not isinstance(vtep, IPv6Network):
            self.fail(f"The vtep prefix {vtep} must be IPv6.")
            vtep = None
        public_ranges = self.public_ranges(by_role.get("public", []))

        asns = self.snapshot["asn_list"]
        overlay = self.one([a["asn"] for a in asns if _tagged(a, "overlay")], "ASN tagged overlay")
        handoff_asn = self.one([a["asn"] for a in asns if _tagged(a, "public-handoff")], "ASN tagged public-handoff")

        handoff = None
        if handoff_vid is not None:
            prefixes = by_vlan.get(handoff_vid, [])
            prefix4 = self.one([p for p in prefixes if p.version == 4], f"IPv4 prefix on handoff VLAN {handoff_vid}")
            prefix6 = self.one([p for p in prefixes if p.version == 6], f"IPv6 prefix on handoff VLAN {handoff_vid}")
            if isinstance(prefix4, IPv4Network) and isinstance(prefix6, IPv6Network) and handoff_asn is not None:
                handoff = PublicHandoff(
                    vlan_id=handoff_vid,
                    prefix4=prefix4,
                    prefix6=prefix6,
                    asn=handoff_asn,
                    public_ranges=tuple(public_ranges),
                )
        if mgmt is None or overlay is None or vtep is None or handoff is None:
            return None
        return Fabric(
            overlay_asn=overlay,
            mgmt_vlan_id=mgmt,
            vtep_prefix=vtep,
            underlay=tuple(underlay),
            handoff=handoff,
        )

    def public_ranges(self, prefixes: list[Obj]) -> list[PublicRange]:
        if not prefixes:
            self.fail("No prefix has role public. Add the public ranges.")
        ranges = []
        for p in sorted(prefixes, key=lambda p: _net_key(ip_network(p["prefix"]))):
            max_len = (p.get("custom_fields") or {}).get("max_subnet_length")
            if max_len is None:
                self.fail(f"Public prefix {p['prefix']} has no max_subnet_length. Set the custom field.")
                continue
            try:
                ranges.append(PublicRange(prefix=ip_network(p["prefix"]), max_len=max_len))
            except ValidationError as exc:
                self.fail(_messages(exc)[0])
        for a in ranges:
            for b in ranges:
                if a is not b and _inside(a.prefix, b.prefix):
                    self.fail(f"Public prefix {a.prefix} is inside public prefix {b.prefix}. Keep only one.")
        return ranges

    # --- Devices --------------------------------------------------------------------------------------------------

    def devices(self) -> tuple[list[Core], list[Leaf], list[Node]]:
        asn_by_id = {str(a["id"]): a["asn"] for a in self.snapshot["asn_list"]}
        named = []
        for device in self.snapshot["device_list"]:
            if not device.get("name"):
                self.fail(f"A device with role {device['role']['slug']} has no name. Name it.")
            else:
                named.append(device)
        leaf_names = {d["name"] for d in named if d["role"]["slug"] == "leaf"}
        cores: list[Core] = []
        leafs: list[Leaf] = []
        nodes: list[Node] = []
        for device in sorted(named, key=lambda d: natural_key(d["name"])):
            role = device["role"]["slug"]
            platform = (device.get("platform") or {}).get("slug")
            if platform != PLATFORMS[role]:
                self.fail(f"{device['name']} has role {role}, so its platform must be {PLATFORMS[role]}.")
                continue
            d = _Device(self, device, asn_by_id)
            if role == "core":
                cores += [c for c in [d.core()] if c is not None]
            elif role == "leaf":
                leafs += [lf for lf in [d.leaf(leaf_names - {device["name"]})] if lf is not None]
            else:
                nodes += [n for n in [d.node()] if n is not None]
        return cores, leafs, nodes


@dataclass
class _Lag:
    """A LAG as read, before deciding what its peer must be. `lag` is None when it can't be built."""

    lag: Lag | None
    peers: set[str]


@dataclass
class _Device:
    builder: _Builder
    device: Obj
    asn_by_id: dict[str, int]

    @property
    def name(self) -> str:
        return str(self.device["name"])

    @property
    def managed(self) -> bool:
        return bool((self.device.get("custom_fields") or {}).get("sharpwire_managed"))

    @property
    def interfaces(self) -> list[Obj]:
        return sorted(self.device["interfaces"], key=lambda i: natural_key(i["name"]))

    def fail(self, problem: str) -> None:
        self.builder.fail(f"{self.name}: {problem}")

    def vid(self, iface: Obj, vlan: Obj) -> int | None:
        """The VID of a VLAN an interface uses, which must be a fabric VLAN."""
        vid = self.builder.fabric_vlans.get(str(vlan["id"]))
        if vid is None:
            self.fail(f"{iface['name']} uses VLAN {vlan['vid']}, which isn't a fabric VLAN tagged sharpwire-managed.")
        return vid

    def asn_and_router_id(self) -> tuple[int, IPv4Address] | None:
        cf = self.device.get("custom_fields") or {}
        asn = None
        if cf.get("asn") is None:
            self.fail("set the asn custom field.")
        else:
            asn = self.asn_by_id.get(str(cf["asn"]))
            if asn is None:
                self.fail("its asn custom field points at an ASN that isn't tagged sharpwire-managed.")
        router_id = None
        if not cf.get("router_id"):
            self.fail("set the router_id custom field.")
        else:
            try:
                router_id = IPv4Address(cf["router_id"])
            except ValueError:
                self.fail(f"router_id {cf['router_id']!r} isn't an IPv4 address.")
        if asn is None or router_id is None:
            return None
        return asn, router_id

    def addresses(self, iface: Obj) -> tuple[IPv4Interface | IPv6Interface, ...] | None:
        """The interface's active addresses, at most one per family."""
        active = [ip_interface(a["address"]) for a in iface["ip_addresses"] if a.get("status") == "active"]
        for version, count in Counter(a.version for a in active).items():
            if count > 1:
                self.fail(f"{iface['name']} has {count} active IPv{version} addresses. Keep one active.")
                return None
        return tuple(sorted(active, key=_address_key))

    def vlan_interfaces(self, *, parent: str | None = None) -> list[VlanInterface] | None:
        """Virtual interfaces with an untagged VLAN (the VLAN they're on), optionally only those under `parent`."""
        out = []
        ok = True
        for i in self.interfaces:
            if i["type"] != "virtual" or i.get("untagged_vlan") is None:
                continue
            if parent is not None and (i.get("parent") or {}).get("name") != parent:
                continue
            vid = self.vid(i, i["untagged_vlan"])
            addresses = self.addresses(i)
            if vid is None or addresses is None:
                ok = False
                continue
            out.append(VlanInterface(name=i["name"], vlan_id=vid, mtu=i.get("mtu"), addresses=addresses))
        if not ok:
            return None
        return sorted(out, key=lambda v: (v.vlan_id, natural_key(v.name)))

    def lags(self) -> list[_Lag]:
        """Every LAG, with the devices its members connect to (across patch panels)."""
        out = []
        for i in self.interfaces:
            if i["type"] != "lag":
                continue
            members = [m for m in self.interfaces if (m.get("lag") or {}).get("name") == i["name"]]
            peers = {e["device"]["name"] for m in members for e in m.get("connected_endpoints") or [] if e}
            ok = True
            if not members:
                self.fail(f"LAG {i['name']} has no member interfaces.")
                ok = False
            if i.get("mtu") is None:
                self.fail(f"set the MTU of LAG {i['name']}.")
                ok = False
            if i.get("mode") != "tagged":
                self.fail(f"set the 802.1Q mode of LAG {i['name']} to tagged.")
                ok = False
            ports = [self.port(m) for m in members]
            vids = [self.vid(i, v) for v in i.get("tagged_vlans") or []]
            if not ok or None in ports or None in vids:
                out.append(_Lag(None, peers))
                continue
            lag = Lag(
                name=i["name"],
                members=tuple(p for p in ports if p is not None),
                mtu=i["mtu"],
                peer=next(iter(peers)) if len(peers) == 1 else None,
                mlag_id=(i.get("custom_fields") or {}).get("mlag_id"),
                tagged_vlans=tuple(sorted(v for v in vids if v is not None)),
            )
            out.append(_Lag(lag, peers))
        return out

    def port(self, i: Obj) -> Port | None:
        l2mtu = (i.get("custom_fields") or {}).get("l2mtu")
        if i.get("mtu") is None or l2mtu is None:
            self.fail(f"set the MTU and the l2mtu custom field on {i['name']}.")
            return None
        return Port(name=i["name"], mtu=i["mtu"], l2mtu=l2mtu)

    def core(self) -> Core | None:
        ids = self.asn_and_router_id()
        lags = self.lags()
        if len(lags) != 1:
            self.fail(f"a core needs exactly one LAG, found {len(lags) or 'none'}.")
            return None
        lag = lags[0].lag
        if lag is None:
            return None
        on_lag = self.vlan_interfaces(parent=lag.name)
        elsewhere = [
            i["name"]
            for i in self.interfaces
            if i["type"] == "virtual"
            and i.get("untagged_vlan") is not None
            and (i.get("parent") or {}).get("name") != lag.name
        ]
        if elsewhere:
            self.fail(f"set the parent of {', '.join(elsewhere)} to LAG {lag.name}.")
        if ids is None or on_lag is None or elsewhere:
            return None
        return Core(
            name=self.name, managed=self.managed, asn=ids[0], router_id=ids[1], lag=lag, vlan_interfaces=tuple(on_lag)
        )

    def leaf(self, other_leafs: set[str]) -> Leaf | None:
        bridges = [i["name"] for i in self.interfaces if i["type"] == "bridge"]
        if len(bridges) != 1:
            self.fail(f"a leaf needs exactly one bridge interface, found {len(bridges) or 'none'}.")
        lags = self.lags()
        peer_links = [lag for lag in lags if lag.peers & other_leafs]
        if len(peer_links) != 1:
            self.fail(f"a leaf needs exactly one LAG cabled to the other leaf, found {len(peer_links) or 'none'}.")
        bonds = [lag for lag in lags if lag not in peer_links]
        for bond in bonds:
            if bond.lag is not None and len(bond.peers) != 1:
                reached = ", ".join(sorted(bond.peers)) or "no device"
                self.fail(f"LAG {bond.lag.name}'s members must connect to one device; they reach {reached}.")
        mgmt = self.vlan_interfaces(parent=bridges[0]) if len(bridges) == 1 else None
        if mgmt is not None and len(mgmt) != 1:
            self.fail(f"a leaf needs exactly one VLAN interface on its bridge (mgmt), found {len(mgmt) or 'none'}.")
        if (
            len(bridges) != 1
            or len(peer_links) != 1
            or peer_links[0].lag is None
            or any(b.lag is None or len(b.peers) != 1 for b in bonds)
            or mgmt is None
            or len(mgmt) != 1
        ):
            return None
        return Leaf(
            name=self.name,
            managed=self.managed,
            bridge=bridges[0],
            peer_link=peer_links[0].lag,
            bonds=tuple(b.lag for b in bonds if b.lag is not None),
            mgmt=mgmt[0],
        )

    def node(self) -> Node | None:
        ids = self.asn_and_router_id()
        loopbacks = [
            a
            for i in self.interfaces
            if i["name"] == LOOPBACK
            for a in (self.addresses(i) or ())
            if isinstance(a, IPv6Interface)
        ]
        if len(loopbacks) != 1:
            self.fail(f"a node needs exactly one active IPv6 address on {LOOPBACK}, found {len(loopbacks) or 'none'}.")
        vlan_interfaces = self.vlan_interfaces()
        if ids is None or len(loopbacks) != 1 or vlan_interfaces is None:
            return None
        return Node(
            name=self.name,
            managed=self.managed,
            asn=ids[0],
            router_id=ids[1],
            loopback=loopbacks[0],
            vlan_interfaces=tuple(vlan_interfaces),
        )


def build_network(snapshot: Snapshot) -> Network:
    """The intent model for the network NetBox describes. Raises BuildError with everything to fix."""
    builder = _Builder(snapshot)
    fabric = builder.fabric()
    cores, leafs, nodes = builder.devices()
    if builder.problems or fabric is None:
        raise BuildError(builder.problems)
    try:
        return Network(fabric=fabric, cores=tuple(cores), leafs=tuple(leafs), nodes=tuple(nodes))
    except ValidationError as exc:
        raise BuildError(_messages(exc)) from exc


def _tagged(obj: Obj, slug: str) -> bool:
    return any(t["slug"] == slug for t in obj.get("tags") or [])


def _inside(a: IPv4Network | IPv6Network, b: IPv4Network | IPv6Network) -> bool:
    if isinstance(a, IPv4Network) and isinstance(b, IPv4Network):
        return a.subnet_of(b)
    if isinstance(a, IPv6Network) and isinstance(b, IPv6Network):
        return a.subnet_of(b)
    return False


def _net_key(net: IPv4Network | IPv6Network) -> tuple[int, int, int]:
    return (net.version, int(net.network_address), net.prefixlen)


def _address_key(address: IPv4Interface | IPv6Interface) -> tuple[int, int]:
    # IPv6 first: the fabric is IPv6-first, and the network doc lists addresses that way.
    return (-address.version, int(address.ip))


def _messages(exc: ValidationError) -> list[str]:
    """One sentence per problem. The model's validators put one problem on each line."""
    return [
        line
        for error in exc.errors()
        for line in str(error["msg"]).removeprefix("Value error, ").splitlines()
        if line.strip()
    ]
