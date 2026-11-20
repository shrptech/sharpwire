"""RouterOS desired state as a list of resources, keyed by natural keys (never `.id`, which isn't stable).

A resource is one row of a RouterOS menu, such as `/interface/vlan` name=vlan101. Its `kind` says how the
diff may treat it:

- `item`: sharpwire adds, changes and removes it.
- `baseline`: it always exists on the device (a physical port). sharpwire only sets fields, and never removes it.
- `settings`: a menu with a single row and no key (e.g. `/interface/bridge/mlag`). sharpwire only sets fields.

Ordered menus (routing filter rules) are identified by chain plus a sharpwire comment, which the driver keeps unique
(RouterOS itself accepts duplicate comments). Their `position` within the chain is kept separately: it isn't part of
the identity, so inserting a rule doesn't change the identity of the rules after it. On the device, order comes
from `.nextid` (spikes/routeros/FINDINGS.md, Q5).

Property names are RouterOS 7 names, verified on RouterOS 7.25.1: every line of the core golden file was accepted
by a CHR (spikes/routeros/FINDINGS.md). Any correction belongs here and in render.py, nowhere else.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ResourceKind = Literal["item", "baseline", "settings"]


@dataclass(frozen=True)
class Resource:
    path: str
    fields: tuple[tuple[str, str], ...]
    key_fields: tuple[str, ...] = ("name",)
    kind: ResourceKind = "item"
    position: int | None = None

    def __post_init__(self) -> None:
        names = [k for k, _ in self.fields]
        missing = [k for k in self.key_fields if k not in names]
        if missing:
            raise ValueError(f"{self.path}: key fields {missing} are missing from the fields")
        if len(set(names)) != len(names):
            raise ValueError(f"{self.path}: a field appears twice")

    @property
    def key(self) -> tuple[tuple[str, str], ...]:
        values = dict(self.fields)
        return tuple((k, values[k]) for k in self.key_fields)

    @property
    def identity(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        """What the diff matches a live row on: the menu and the natural key."""
        return (self.path, self.key)


def item(path: str, *fields: tuple[str, str], key: tuple[str, ...] = ("name",)) -> Resource:
    return Resource(path=path, fields=tuple(fields), key_fields=key)


def baseline(path: str, *fields: tuple[str, str], key: tuple[str, ...] = ("name",)) -> Resource:
    return Resource(path=path, fields=tuple(fields), key_fields=key, kind="baseline")


def settings(path: str, *fields: tuple[str, str]) -> Resource:
    return Resource(path=path, fields=tuple(fields), key_fields=(), kind="settings")


RULE_COMMENT_PREFIX = "sharpwire:"


def ordered(path: str, chain: str, name: str, position: int, *fields: tuple[str, str]) -> Resource:
    """A row in an ordered menu, identified by its chain and a comment `sharpwire:<chain>:<name>`."""
    comment = f"{RULE_COMMENT_PREFIX}{chain}:{name}"
    return Resource(
        path=path,
        fields=(("chain", chain), *fields, ("comment", comment)),
        key_fields=("chain", "comment"),
        position=position,
    )


def _quote(value: str) -> str:
    if value and all(c.isalnum() or c in "-_.:/,=" for c in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def _assignments(fields: Iterable[tuple[str, str]]) -> str:
    return " ".join(f"{k}={_quote(v)}" for k, v in fields)


def to_rsc(resources: Iterable[Resource]) -> str:
    """Render resources as a RouterOS script, for human review and golden tests. The driver itself uses REST."""
    lines: list[str] = []
    current_path: str | None = None
    for r in resources:
        if r.path != current_path:
            lines.append("/" + r.path.replace("/", " "))
            current_path = r.path
        if r.kind == "item":
            lines.append(f"add {_assignments(r.fields)}")
        elif r.kind == "settings":
            lines.append(f"set {_assignments(r.fields)}")
        else:
            where = " ".join(f"{k}={_quote(v)}" for k, v in r.key)
            rest = [(k, v) for k, v in r.fields if k not in r.key_fields]
            lines.append(f"set [find {where}] {_assignments(rest)}")
    return "\n".join(lines) + "\n"
