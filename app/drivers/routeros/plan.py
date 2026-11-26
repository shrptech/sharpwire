"""Plans for cores and leafs: the diff, its inverse, the hard guard and the warnings.

The hard guard (docs/03-pipeline-and-validation.md#hard-guards) covers the controller's mgmt path on the device:
- the mgmt VLAN interface and its addresses;
- what it sits on, now and once applied. On a core that's the bond and its member ports; on a leaf the bridge, the
  bridge VLAN rows carrying the mgmt VLAN, and the bridge ports and bonds that carry it.

A plan may not remove any of those, change a bond's mode or members, disable any of them, turn a bridge's VLAN
filtering, or change the mgmt bridge VLAN or a carrying bridge port. Other changes there (MTU, LACP rate) are
warnings. A set that only adds sharpwire's marker (adopting a hand-bootstrapped row) is neither.

Warnings (docs/02-intent-model.md#plan-warnings): the mgmt path as above; a core's BGP instance; a leaf's MLAG config,
the peer-link and its VLANs. Each kind of warning appears once per plan.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.drivers.routeros import diff as routeros_diff
from app.drivers.routeros.diff import MENUS, LiveState
from app.drivers.routeros.fields import FIELDS, normalise
from app.drivers.routeros.render import render_core, render_leaf
from app.drivers.routeros.resources import Resource
from app.intent.models import Core, Leaf, Network
from app.schemas.plan import Op, PlanDraft, PlanWarning

CORE_MENUS = (
    "interface/ethernet",
    "interface/bonding",
    "interface/vlan",
    "ipv6/address",
    "ip/address",
    "routing/bgp/instance",
    "routing/bfd/configuration",
    "routing/filter/rule",
    "routing/bgp/connection",
)
LEAF_MENUS = (
    "interface/ethernet",
    "interface/bonding",
    "interface/bridge",
    "interface/bridge/mlag",
    "interface/bridge/port",
    "interface/bridge/vlan",
    "interface/vlan",
    "ipv6/address",
    "ip/address",
)
ADDRESS_MENUS = ("ipv6/address", "ip/address")


def fingerprint(live: LiveState, menus: Iterable[str]) -> str:
    """A hash of the managed fields of every row in the device's menus, for the stale check.

    Counters and other fields sharpwire doesn't manage are left out, so only a change that could alter the plan
    changes the fingerprint. Order counts only where RouterOS evaluates it: within each chain of an ordered menu.
    """
    canonical: dict[str, list[dict[str, str | None]]] = {}
    for path in MENUS:
        if path not in set(menus):
            continue
        rows = [
            {name: normalise(spec, row.get(name, spec.default)) for name, spec in FIELDS[path].items()}
            for row in routeros_diff.live_rows(live, path)
        ]
        menu = MENUS[path]
        if menu.ordered:
            rows.sort(key=lambda r: r["chain"] or "")
        else:
            rows.sort(key=lambda r: [r[k] or "" for k in menu.key])
        canonical[path] = rows
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _adoption_only(op: Op) -> bool:
    return op.action == "set" and set(op.fields) == {"comment"}


def _interface_of(op: Op) -> set[str | None]:
    return {op.key.get("interface"), op.fields.get("interface"), op.before.get("interface")} - {None}


def _names(value: str | None) -> set[str]:
    return set((value or "").split(",")) - {""}


def _vlan_ids(value: str | None) -> set[int]:
    """`10`, `10,20` or `100-110`, as RouterOS writes bridge VLAN IDs."""
    ids: set[int] = set()
    for part in _names(value):
        low, _, high = part.partition("-")
        if low.isdigit() and (not high or high.isdigit()):
            ids.update(range(int(low), int(high or low) + 1))
    return ids


def _rows(desired: Sequence[Resource], live: LiveState, path: str) -> list[dict[str, str]]:
    """Every rendered and live row of a menu: the mgmt path is what it is now and what it will be."""
    return [dict(r.fields) for r in desired if r.path == path] + [dict(r) for r in routeros_diff.live_rows(live, path)]


@dataclass
class MgmtPath:
    interface: str
    vlan_id: int
    parents: set[str] = field(default_factory=set)
    bonds: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)
    bridge_ports: set[str] = field(default_factory=set)


def mgmt_path(desired: Sequence[Resource], live: LiveState, mgmt: str, vlan_id: int) -> MgmtPath:
    path = MgmtPath(interface=mgmt, vlan_id=vlan_id)
    path.parents = {
        r["interface"] for r in _rows(desired, live, "interface/vlan") if r.get("name") == mgmt and r.get("interface")
    }
    bridges = path.parents & {r.get("name") for r in _rows(desired, live, "interface/bridge")}
    carriers: set[str] = set()
    for r in _rows(desired, live, "interface/bridge/vlan"):
        if r.get("bridge") in bridges and vlan_id in _vlan_ids(r.get("vlan-ids")):
            carriers |= _names(r.get("tagged")) - bridges
    path.bridge_ports = carriers
    bond_names = {r.get("name") for r in _rows(desired, live, "interface/bonding")}
    path.bonds = {n for n in (path.parents | carriers) if n in bond_names}
    for r in _rows(desired, live, "interface/bonding"):
        if r.get("name") in path.bonds:
            path.ports |= _names(r.get("slaves"))
    return path


# Changes on the mgmt path that can cut the session, per menu. Anything else there is a warning.
_STRUCTURE = {
    "interface/bonding": {"mode", "slaves", "disabled"},
    "interface/bridge": {"vlan-filtering", "disabled"},
    "interface/ethernet": {"disabled"},
    "interface/bridge/port": {"bridge", "frame-types", "disabled"},
}


def _mgmt_checks(target: str, ops: Sequence[Op], path: MgmtPath) -> tuple[list[str], list[PlanWarning]]:
    violations: list[str] = []
    warned: list[str] = []
    mgmt = path.interface

    def guard(what: str) -> None:
        violations.append(
            f"{target}: the plan changes {what}, which the controller's management session depends on. "
            "Make NetBox match the device instead."
        )

    def on_path(op: Op) -> str | None:
        name = op.key.get("name")
        if op.path == "interface/bonding" and name in path.bonds:
            return f"the bond {name}"
        if op.path == "interface/bridge" and name in path.parents:
            return f"the bridge {name}"
        if op.path == "interface/ethernet" and name in path.ports:
            return f"the port {name}"
        if (
            op.path == "interface/bridge/port"
            and op.key.get("bridge") in path.parents
            and op.key.get("interface") in path.bridge_ports
        ):
            return f"the bridge port {op.key['interface']}"
        return None

    for op in ops:
        if _adoption_only(op):
            continue
        if op.path == "interface/vlan" and op.key == {"name": mgmt}:
            if op.action == "set" and set(op.fields) <= {"mtu", "comment"}:
                warned.append(f"the mgmt interface {mgmt}'s MTU")
            else:
                guard(f"the mgmt interface {mgmt}")
        elif op.path in ADDRESS_MENUS and mgmt in _interface_of(op):
            guard(f"an address on the mgmt interface {mgmt} ({op.key['address']})")
        elif op.path == "interface/bridge/vlan" and path.vlan_id in _vlan_ids(op.key.get("vlan-ids")):
            if op.key.get("bridge") in path.parents:
                guard(f"the bridge VLAN carrying mgmt VLAN {path.vlan_id}")
        elif op.action == "remove" and op.path.startswith("interface/") and op.key.get("name") in path.parents:
            guard(f"{op.key['name']}, the interface {mgmt} sits on")
        elif (what := on_path(op)) is not None:
            if op.action == "remove" or set(op.fields) & _STRUCTURE.get(op.path, set()):
                guard(f"{what} on the mgmt path")
            elif op.action != "add":
                warned.append(what)
    warnings = []
    if warned:
        warnings.append(PlanWarning(code="mgmt_path", message=f"{target}: the mgmt path changes: {', '.join(warned)}."))
    return violations, warnings


def _draft(
    target: str, desired: list[Resource], live: LiveState, menus: Sequence[str], mgmt: str, mgmt_vlan_id: int
) -> tuple[PlanDraft, list[Op]]:
    result = routeros_diff.diff(desired, live, menus)
    ops = list(result.ops)
    violations, warnings = _mgmt_checks(target, ops, mgmt_path(desired, live, mgmt, mgmt_vlan_id))
    draft = PlanDraft(
        target=target,
        ops=result.ops,
        inverse_ops=routeros_diff.inverse(live, result.ops, menus),
        fingerprint=fingerprint(live, menus),
        warnings=tuple(warnings),
        guard_violations=tuple(violations),
        unmanaged=result.unmanaged,
    )
    return draft, ops


def plan_core(network: Network, core: Core, live: LiveState) -> PlanDraft:
    vlan_id = network.fabric.mgmt_vlan_id
    mgmt = core.vlan_interface(vlan_id).name
    draft, ops = _draft(core.name, render_core(network, core), live, CORE_MENUS, mgmt, vlan_id)
    warnings = list(draft.warnings)
    if any(op.path == "routing/bgp/instance" and not _adoption_only(op) for op in ops):
        warnings.append(
            PlanWarning(code="core_bgp_instance", message=f"{core.name}: the BGP instance changes (ASN or router-id).")
        )
    return draft.model_copy(update={"warnings": tuple(warnings)})


def plan_leaf(network: Network, leaf: Leaf, live: LiveState) -> PlanDraft:
    desired = render_leaf(network, leaf)
    draft, ops = _draft(leaf.name, desired, live, LEAF_MENUS, leaf.mgmt.name, leaf.mgmt.vlan_id)
    warnings = list(draft.warnings)
    peer_link = leaf.peer_link.name
    rendered_vlans = {r.key_dict["vlan-ids"]: dict(r.fields) for r in desired if r.path == "interface/bridge/vlan"}
    mlag = peer_vlans = False
    changed_vlans: list[str] = []
    for op in ops:
        if _adoption_only(op):
            continue
        bond = op.path == "interface/bonding" and (op.key == {"name": peer_link} or "mlag-id" in op.fields | op.before)
        port = op.path == "interface/bridge/port" and op.key.get("interface") == peer_link
        if op.path == "interface/bridge/mlag" or bond or port:
            mlag = True
        elif op.path == "interface/bridge/vlan":
            vlan = op.key["vlan-ids"]
            carried = _names(op.fields.get("tagged")) | _names(op.before.get("tagged"))
            carried |= _names(rendered_vlans.get(vlan, {}).get("tagged"))
            if peer_link in carried:
                peer_vlans = True
                changed_vlans.append(vlan)
    if mlag:
        warnings.append(
            PlanWarning(code="leaf_mlag", message=f"{leaf.name}: the MLAG config or the peer-link changes.")
        )
    if peer_vlans:
        vlans = sorted(changed_vlans, key=_sort_vlan)
        warnings.append(
            PlanWarning(
                code="leaf_peer_link_vlans",
                message=f"{leaf.name}: the peer-link's VLANs change ({', '.join(vlans)}).",
            )
        )
    return draft.model_copy(update={"warnings": tuple(warnings)})


def _sort_vlan(value: str) -> tuple[int, str]:
    return (min(_vlan_ids(value), default=0), value)
