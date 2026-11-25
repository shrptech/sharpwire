"""A device's REST view of rendered resources, with RouterOS's quirks, for diff tests.

It mimics what spikes/routeros/FINDINGS.md recorded on 7.25.1: every value a string, fields at their default left
out, booleans read back as true/false, `.id` and `.nextid` on every row. Step 3b replaces this with real captures.
"""

from collections.abc import Iterable
from itertools import count

from app.drivers.routeros.diff import MENUS
from app.drivers.routeros.fields import FIELDS, normalise
from app.drivers.routeros.resources import Resource

_BOOL_READBACK = {"yes": "true", "no": "false"}


def device_view(resources: Iterable[Resource]) -> dict[str, list[dict[str, str]]]:
    ids = count(1)
    state: dict[str, list[dict[str, str]]] = {}
    for r in resources:
        row = {".id": f"*{next(ids):X}"}
        for name, value in r.fields:
            spec = FIELDS[r.path][name]
            if spec.default is not None and normalise(spec, value) == normalise(spec, spec.default):
                continue
            row[name] = _BOOL_READBACK.get(value, value) if spec.kind == "bool" else value
        state.setdefault(r.path, []).append(row)
    return link(state)


def link(state: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    """Set `.nextid` from list order, as the device does."""
    for rows in state.values():
        for this, following in zip(rows, [*rows[1:], None][: len(rows)], strict=True):
            if ".id" in this:
                this[".nextid"] = following[".id"] if following is not None else "*ffffffff"
    return state


def row(state: dict[str, list[dict[str, str]]], path: str, **key: str) -> dict[str, str]:
    """The live row with these key fields, for a test to change."""
    assert path in MENUS
    matches = [r for r in state[path] if all(r.get(k.replace("_", "-")) == v for k, v in key.items())]
    assert len(matches) == 1, (path, key, matches)
    return matches[0]
