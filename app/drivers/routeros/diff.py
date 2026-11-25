"""Diff the rendered RouterOS resources against a device's live rows, into ordered plan ops.

Live state is what the REST API returns per menu: a list of rows, all values strings, fields at their default left
out, `.id` and `.nextid` included. The diff never uses `.id` for identity: rows match on natural keys, normalised
(fields.py), and `.nextid` only gives the order of an ordered menu.

What it does with each row (docs/04-drivers.md#routeros-cores-and-leafs):
- a rendered row with no live match is added, and a matched one with different managed fields is set. A match
  without the marker is adopted, by setting its comment;
- a live row carrying sharpwire's marker that's no longer rendered is removed;
- any other live row is unmanaged: reported, never changed;
- baseline rows (physical ports) and settings menus are only ever set. A missing one is a problem;
- dynamic rows are invisible.

Op order is make-before-break: adds and sets in dependency order (referenced objects first), the ordered menu's
moves with its adds, then removes in reverse dependency order (dependents first).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import count

from app.drivers.routeros.fields import FIELDS, Field, normalise
from app.drivers.routeros.resources import Resource, ResourceKind, owned
from app.schemas.plan import Op, Unmanaged

LiveRow = Mapping[str, str]
# Menu path -> its rows. A settings menu may be given as its single row.
LiveState = Mapping[str, Sequence[LiveRow] | LiveRow]


@dataclass(frozen=True)
class Menu:
    kind: ResourceKind
    key: tuple[str, ...]
    ordered: bool = False


# Every menu sharpwire renders into, in dependency order: a row may only reference rows in earlier menus.
MENUS: Mapping[str, Menu] = {
    "interface/ethernet": Menu("baseline", ("name",)),
    "interface/bonding": Menu("item", ("name",)),
    "interface/bridge": Menu("item", ("name",)),
    "interface/bridge/mlag": Menu("settings", ()),
    "interface/bridge/port": Menu("item", ("bridge", "interface")),
    "interface/bridge/vlan": Menu("item", ("bridge", "vlan-ids")),
    "interface/vlan": Menu("item", ("name",)),
    "ipv6/address": Menu("item", ("address", "interface")),
    "ip/address": Menu("item", ("address", "interface")),
    "routing/bgp/instance": Menu("item", ("name",)),
    "routing/bfd/configuration": Menu("item", ("interfaces",)),
    "routing/filter/rule": Menu("item", ("chain", "comment"), ordered=True),
    "routing/bgp/connection": Menu("item", ("name",)),
}


class LiveStateError(Exception):
    """The device lacks something sharpwire can't create, such as a physical port. Every problem, at once."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(" ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Diff:
    ops: tuple[Op, ...]
    unmanaged: tuple[Unmanaged, ...]


# --- Rows --------------------------------------------------------------------------------------------------------


def live_rows(live: LiveState, path: str) -> list[LiveRow]:
    rows = live.get(path, [])
    if isinstance(rows, Mapping):
        rows = [rows]
    return table_order([r for r in rows if r.get("dynamic") not in ("true", "yes")])


def table_order(rows: Sequence[LiveRow]) -> list[LiveRow]:
    """Rows in the device's order, following `.nextid` when every row has one, else as listed."""
    by_id = {r[".id"]: r for r in rows if ".id" in r}
    if len(by_id) != len(rows) or not all(".nextid" in r for r in rows):
        return list(rows)
    pointed = {r[".nextid"] for r in rows}
    heads = [i for i in by_id if i not in pointed]
    if len(heads) != 1:
        return list(rows)
    ordered: list[LiveRow] = []
    current: str | None = heads[0]
    while current in by_id and len(ordered) <= len(rows):
        ordered.append(by_id[current])
        current = by_id[current].get(".nextid")
    return ordered if len(ordered) == len(rows) else list(rows)


def _spec(path: str) -> Mapping[str, Field]:
    return FIELDS[path]


def _effective(path: str, values: Mapping[str, str], name: str) -> str | None:
    return values.get(name, _spec(path)[name].default)


def _compared(path: str, values: Mapping[str, str], name: str) -> str | None:
    return normalise(_spec(path)[name], _effective(path, values, name))


def _identity(path: str, values: Mapping[str, str]) -> tuple[str | None, ...]:
    return tuple(_compared(path, values, k) for k in MENUS[path].key)


def _key(path: str, values: Mapping[str, str]) -> dict[str, str]:
    return {k: values.get(k, "") for k in MENUS[path].key}


def _managed(path: str, values: Mapping[str, str]) -> dict[str, str | None]:
    return {name: values[name] for name in _spec(path) if name in values}


def _check(resource: Resource) -> None:
    menu = MENUS.get(resource.path)
    if menu is None or menu.kind != resource.kind or menu.key != resource.key_fields:
        raise ValueError(f"{resource.path}: not a menu the diff knows, or its kind or key differs")
    unknown = [name for name, _ in resource.fields if name not in _spec(resource.path)]
    if unknown:
        raise ValueError(f"{resource.path}: fields {unknown} have no entry in fields.FIELDS")


# --- The diff ----------------------------------------------------------------------------------------------------


def _changes(path: str, want: Resource, row: LiveRow) -> tuple[dict[str, str | None], dict[str, str | None]]:
    desired = dict(want.fields)
    new: dict[str, str | None] = {}
    old: dict[str, str | None] = {}
    for name in _spec(path):
        if name in want.key_fields:
            continue
        if _compared(path, desired, name) != _compared(path, row, name):
            new[name] = desired.get(name)
            old[name] = row.get(name)
    return new, old


def _longest_in_order(indexes: list[int]) -> set[int]:
    """The positions in `indexes` forming its longest increasing run: rules already in the right order."""
    tails: list[int] = []
    tail_at: list[int] = []
    previous = [-1] * len(indexes)
    for i, value in enumerate(indexes):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tails):
            tails.append(value)
            tail_at.append(i)
        else:
            tails[lo] = value
            tail_at[lo] = i
        previous[i] = tail_at[lo - 1] if lo else -1
    keep: set[int] = set()
    i = tail_at[-1] if tail_at else -1
    while i != -1:
        keep.add(i)
        i = previous[i]
    return keep


def _placements(path: str, wants: list[Resource], matched: dict[int, LiveRow], rows: list[LiveRow]) -> list[Op]:
    """Adds and moves for an ordered menu, chain by chain, so each chain ends in its rendered order.

    Rules already in the right relative order stay put. Working backwards from the chain's last rule, every other
    rule is added or moved right before the rule that follows it, which is already where it ends up.
    """
    ops: list[Op] = []
    live_index = {id(r): i for i, r in enumerate(rows)}
    chains: dict[str, list[int]] = {}
    for i, want in enumerate(wants):
        chains.setdefault(dict(want.fields)["chain"], []).append(i)
    for members in chains.values():
        members.sort(key=lambda i: wants[i].position or 0)
        existing = sorted((i for i in members if i in matched), key=lambda i: live_index[id(matched[i])])
        rank = {i: n for n, i in enumerate(members)}
        stay = {existing[n] for n in _longest_in_order([rank[i] for i in existing])}
        following: Resource | None = None
        for i in reversed(members):
            want = wants[i]
            place = None if following is None else following.key_dict
            if i not in matched:
                ops.append(Op(action="add", path=path, key=want.key_dict, fields=dict(want.fields), place_before=place))
            elif i not in stay:
                ops.append(Op(action="move", path=path, key=want.key_dict, place_before=place))
            following = want
    return ops


def _diff_menu(
    path: str, wants: list[Resource], live: LiveState, problems: list[str]
) -> tuple[list[Op], list[Op], list[Unmanaged]]:
    menu = MENUS[path]
    rows = live_rows(live, path)
    forward: list[Op] = []
    removes: list[Op] = []
    unmanaged: list[Unmanaged] = []

    if menu.kind == "settings":
        if not rows:
            problems.append(f"The device has no /{path} settings.")
            return forward, removes, unmanaged
        for want in wants:
            new, old = _changes(path, want, rows[0])
            if new:
                forward.append(Op(action="set", path=path, key={}, fields=new, before=old))
        return forward, removes, unmanaged

    by_identity: dict[tuple[str | None, ...], LiveRow] = {}
    extras: list[LiveRow] = []
    for row in rows:
        identity = _identity(path, row)
        if identity in by_identity:
            extras.append(row)
        else:
            by_identity[identity] = row

    matched: dict[int, LiveRow] = {}
    for i, want in enumerate(wants):
        wanted = _identity(path, dict(want.fields))
        if wanted not in by_identity:
            if menu.kind == "baseline":
                where = " ".join(f"{k}={v}" for k, v in want.key)
                problems.append(f"The device has no /{path} {where}.")
            elif not menu.ordered:
                forward.append(Op(action="add", path=path, key=want.key_dict, fields=dict(want.fields)))
            continue
        row = matched[i] = by_identity.pop(wanted)
        new, old = _changes(path, want, row)
        if new:
            forward.append(Op(action="set", path=path, key=want.key_dict, fields=new, before=old))

    if menu.ordered:
        forward += _placements(path, wants, matched, rows)

    if menu.kind != "baseline":
        for row in [*by_identity.values(), *extras]:
            if owned(row.get("comment")):
                removes.append(Op(action="remove", path=path, key=_key(path, row), before=_managed(path, row)))
            else:
                unmanaged.append(Unmanaged(path=path, key=_key(path, row)))
    return forward, removes, unmanaged


def diff(desired: Iterable[Resource], live: LiveState, menus: Iterable[str]) -> Diff:
    """Ops that make `live` match `desired` in `menus`, the device role's menus. Other menus are never read."""
    scope = set(menus)
    wants: dict[str, list[Resource]] = {path: [] for path in MENUS if path in scope}
    for resource in desired:
        _check(resource)
        if resource.path not in wants:
            raise ValueError(f"{resource.path} is rendered but isn't one of this device's menus")
        wants[resource.path].append(resource)

    problems: list[str] = []
    forward: list[Op] = []
    removes: list[list[Op]] = []
    unmanaged: list[Unmanaged] = []
    for path, path_wants in wants.items():
        f, r, u = _diff_menu(path, path_wants, live, problems)
        forward += f
        removes.append(r)
        unmanaged += u
    if problems:
        raise LiveStateError(problems)
    return Diff(ops=(*forward, *(op for r in reversed(removes) for op in r)), unmanaged=tuple(unmanaged))


# --- Simulation and inverse ----------------------------------------------------------------------------------------


def simulate(live: LiveState, ops: Iterable[Op], menus: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    """The live state after `ops`, as the device would show it. Used to build inverse ops, and by tests to prove a
    plan converges: diffing the result against the render gives no ops.
    """
    scope = [path for path in MENUS if path in set(menus)]
    state = {path: [dict(r) for r in live_rows(live, path)] for path in scope}
    new_ids = count(1)

    def find(path: str, key: Mapping[str, str]) -> int:
        identity = _identity(path, key)
        for i, row in enumerate(state[path]):
            if _identity(path, row) == identity:
                return i
        raise KeyError(f"/{path} {dict(key)}")

    def place(path: str, row: dict[str, str], before: Mapping[str, str] | None) -> None:
        rows = state[path]
        rows.insert(find(path, before) if before is not None else len(rows), row)

    for op in ops:
        rows = state[op.path]
        if op.action == "add":
            row = {".id": f"*sw{next(new_ids)}", **{k: v for k, v in op.fields.items() if v is not None}}
            place(op.path, row, op.place_before)
        elif op.action == "set":
            row = rows[find(op.path, op.key)] if MENUS[op.path].kind != "settings" else rows[0]
            for name, value in op.fields.items():
                if value is None:
                    row.pop(name, None)
                else:
                    row[name] = value
        elif op.action == "remove":
            del rows[find(op.path, op.key)]
        else:
            row = rows.pop(find(op.path, op.key))
            place(op.path, row, op.place_before)

    for rows in state.values():
        for this, following in zip(rows, [*rows[1:], None][: len(rows)], strict=True):
            if ".id" in this:
                this[".nextid"] = following[".id"] if following is not None and ".id" in following else "*ffffffff"
    return state


def as_resources(live: LiveState, ops: Sequence[Op], menus: Iterable[str]) -> list[Resource]:
    """The live rows a plan touches, plus every row sharpwire owns, as resources. Diffing the post-plan state
    against these gives the inverse ops.
    """
    touched = {(op.path, tuple(_identity(op.path, op.key))) for op in ops if op.key}
    out: list[Resource] = []
    for path in [p for p in MENUS if p in set(menus)]:
        menu = MENUS[path]
        for position, row in enumerate(live_rows(live, path)):
            values = {k: v for k, v in _managed(path, row).items() if v is not None}
            if menu.kind == "settings":
                out.append(Resource(path=path, fields=tuple(values.items()), key_fields=(), kind="settings"))
                continue
            if menu.kind != "baseline" and not owned(row.get("comment")):
                if (path, _identity(path, row)) not in touched:
                    continue
            elif menu.kind == "baseline" and (path, _identity(path, row)) not in touched:
                continue
            out.append(
                Resource(
                    path=path,
                    fields=tuple(values.items()),
                    key_fields=menu.key,
                    kind=menu.kind,
                    position=position if menu.ordered else None,
                )
            )
    return out


def inverse(live: LiveState, ops: Sequence[Op], menus: Sequence[str]) -> tuple[Op, ...]:
    """Ops that, applied after `ops`, put every row they touched back as it was in `live`."""
    if not ops:
        return ()
    after = simulate(live, ops, menus)
    return diff(as_resources(live, ops, menus), after, menus).ops
