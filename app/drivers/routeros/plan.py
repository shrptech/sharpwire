"""Plans for cores and leafs: the diff, its inverse, the hard guard and the warnings.

The hard guard (docs/03-pipeline-and-validation.md#hard-guards): on a RouterOS device, a plan may not touch the mgmt
VLAN interface or its addresses, nor remove the interface it sits on. An MTU change on the mgmt VLAN interface is
allowed, with a warning. A set that only adds sharpwire's marker (adopting a hand-bootstrapped row) is neither.

Warnings (docs/02-intent-model.md#plan-warnings): a core's mgmt VLAN interface and BGP instance; a leaf's MLAG
config and the peer-link's VLANs.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence

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


def _mgmt_checks(
    target: str, ops: Sequence[Op], desired: Sequence[Resource], live: LiveState, mgmt: str
) -> tuple[list[str], list[PlanWarning]]:
    violations: list[str] = []
    warnings: list[PlanWarning] = []
    # What mgmt sits on now, and what it will sit on: removing either cuts the session.
    vlan = next(r for r in desired if r.path == "interface/vlan" and r.key_dict == {"name": mgmt})
    parents: set[str | None] = {dict(vlan.fields)["interface"]}
    parents |= {r.get("interface") for r in routeros_diff.live_rows(live, "interface/vlan") if r.get("name") == mgmt}

    def guard(what: str) -> None:
        violations.append(
            f"{target}: the plan changes {what}, which the controller's management session depends on. "
            "Make NetBox match the device instead."
        )

    for op in ops:
        if _adoption_only(op):
            continue
        if op.path == "interface/vlan" and op.key == {"name": mgmt}:
            if op.action == "set" and set(op.fields) <= {"mtu", "comment"}:
                warnings.append(
                    PlanWarning(code="mgmt_interface", message=f"{target}: the mgmt interface {mgmt}'s MTU changes.")
                )
            else:
                guard(f"the mgmt interface {mgmt}")
        elif op.path in ADDRESS_MENUS and mgmt in _interface_of(op):
            guard(f"an address on the mgmt interface {mgmt} ({op.key['address']})")
        elif op.action == "remove" and op.path.startswith("interface/") and op.key.get("name") in parents:
            guard(f"{op.key['name']}, the interface {mgmt} sits on")
    return violations, warnings


def _draft(
    target: str, desired: list[Resource], live: LiveState, menus: Sequence[str], mgmt: str
) -> tuple[PlanDraft, list[Op]]:
    result = routeros_diff.diff(desired, live, menus)
    ops = list(result.ops)
    violations, warnings = _mgmt_checks(target, ops, desired, live, mgmt)
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
    mgmt = core.vlan_interface(network.fabric.mgmt_vlan_id).name
    draft, ops = _draft(core.name, render_core(network, core), live, CORE_MENUS, mgmt)
    warnings = list(draft.warnings)
    for op in ops:
        if op.path == "routing/bgp/instance" and not _adoption_only(op):
            warnings.append(
                PlanWarning(
                    code="core_bgp_instance", message=f"{core.name}: the BGP instance changes (ASN or router-id)."
                )
            )
    return draft.model_copy(update={"warnings": tuple(warnings)})


def _tagged(fields: dict[str, str | None]) -> set[str]:
    return set((fields.get("tagged") or "").split(",")) - {""}


def plan_leaf(network: Network, leaf: Leaf, live: LiveState) -> PlanDraft:
    draft, ops = _draft(leaf.name, render_leaf(network, leaf), live, LEAF_MENUS, leaf.mgmt.name)
    warnings = list(draft.warnings)
    peer_link = leaf.peer_link.name
    mlag_changes = [
        op
        for op in ops
        if not _adoption_only(op)
        and (
            op.path == "interface/bridge/mlag"
            or (
                op.path == "interface/bonding" and (op.key == {"name": peer_link} or "mlag-id" in op.fields | op.before)
            )
        )
    ]
    if mlag_changes:
        warnings.append(PlanWarning(code="leaf_mlag", message=f"{leaf.name}: the MLAG config changes."))
    peer_vlans = [
        op.key["vlan-ids"]
        for op in ops
        if op.path == "interface/bridge/vlan"
        and not _adoption_only(op)
        and (peer_link in _tagged(op.fields)) != (peer_link in _tagged(op.before))
    ]
    if peer_vlans:
        warnings.append(
            PlanWarning(
                code="leaf_peer_link_vlans",
                message=f"{leaf.name}: the peer-link's VLANs change ({', '.join(sorted(peer_vlans))}).",
            )
        )
    return draft.model_copy(update={"warnings": tuple(warnings)})
