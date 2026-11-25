"""The fields sharpwire manages in each RouterOS menu: how to compare them, and their RouterOS defaults.

RouterOS leaves a field at its default out of a GET (spikes/routeros/FINDINGS.md, Q1), so the diff reads a
missing field as its default. A field sharpwire manages but doesn't render for a row is wanted at its default, so
a value set by hand, or one sharpwire stopped rendering, shows as drift. Fields not listed here are never compared.

These defaults are RouterOS facts, used only to compare. They never reach a device, so they aren't intent rules.
None means "unset": the field is absent when it has no value.

What a CHR on 7.25.1 showed (tests/fixtures/routeros/):
- interface and address menus return every field, defaults included, so their defaults below are as read;
- routing menus leave a field out until it's been written, even with its default value, and `get` doesn't report
  it either. Their defaults are RouterOS's documented ones. That only matters for a field sharpwire doesn't render,
  and an absent field and one written back to its default compare equal, as they should;
- `unset` isn't accepted for these fields, so a field going back to its default is written with the default;
- virtio ports have no `l2mtu`, and a CHR has no `/interface/bridge/mlag`. Both need the hardware lab.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address, ip_interface
from typing import Literal

FieldKind = Literal["str", "bool", "int", "list", "ip", "duration"]


@dataclass(frozen=True)
class Field:
    kind: FieldKind = "str"
    default: str | None = None


STR, BOOL, INT, LIST, IP, DURATION = (Field(k) for k in ("str", "bool", "int", "list", "ip", "duration"))
COMMENT = Field()
DISABLED = Field("bool", "no")

# Menu path -> managed field -> how it compares.
FIELDS: Mapping[str, Mapping[str, Field]] = {
    "interface/ethernet": {
        "name": STR,
        "mtu": Field("int", "1500"),
        # Hardware-dependent, and always shown.
        "l2mtu": INT,
        "disabled": DISABLED,
    },
    "interface/bonding": {
        "name": STR,
        "slaves": LIST,
        "mode": Field("str", "balance-rr"),
        "lacp-rate": Field("str", "30secs"),
        "transmit-hash-policy": Field("str", "layer-2"),
        "mlag-id": INT,
        "mtu": Field("int", "1500"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "interface/bridge": {
        "name": STR,
        "vlan-filtering": Field("bool", "no"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "interface/bridge/mlag": {
        "bridge": STR,
        "peer-port": STR,
    },
    "interface/bridge/port": {
        "bridge": STR,
        "interface": STR,
        "frame-types": Field("str", "admit-all"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "interface/bridge/vlan": {
        "bridge": STR,
        "vlan-ids": LIST,
        "tagged": LIST,
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "interface/vlan": {
        "name": STR,
        "interface": STR,
        "vlan-id": INT,
        "mtu": Field("int", "1500"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "ipv6/address": {
        "address": IP,
        "interface": STR,
        "advertise": Field("bool", "yes"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "ip/address": {
        "address": IP,
        "interface": STR,
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "routing/bgp/instance": {
        "name": STR,
        "as": INT,
        "router-id": IP,
        "comment": COMMENT,
    },
    "routing/bfd/configuration": {
        "interfaces": LIST,
        "min-rx": Field("duration", "200ms"),
        "min-tx": Field("duration", "200ms"),
        "multiplier": Field("int", "5"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "routing/filter/rule": {
        "chain": STR,
        "rule": STR,
        "comment": COMMENT,
        "disabled": DISABLED,
    },
    "routing/bgp/connection": {
        "name": STR,
        "instance": STR,
        "afi": Field("list", "ip"),
        "local.role": STR,
        "local.address": IP,
        "remote.address": IP,
        "remote.as": INT,
        "use-bfd": Field("bool", "no"),
        "nexthop-choice": Field("str", "default"),
        "input.filter": STR,
        "output.filter-chain": STR,
        "output.default-originate": Field("str", "never"),
        "comment": COMMENT,
        "disabled": DISABLED,
    },
}

_BOOLS = {"yes": "yes", "true": "yes", "no": "no", "false": "no"}
_UNITS = {"w": 604_800_000_000, "d": 86_400_000_000, "h": 3_600_000_000, "m": 60_000_000, "s": 1_000_000}
_UNITS |= {"ms": 1_000, "us": 1}


def _duration_us(value: str) -> int:
    """RouterOS times: `300ms`, `1s`, `1m30s`, `00:01:30`, or a bare number of seconds."""
    if ":" in value:
        hours, minutes, seconds = value.split(":")
        return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1_000_000)
    total, number = 0, ""
    i = 0
    while i < len(value):
        c = value[i]
        if c.isdigit() or c == ".":
            number += c
            i += 1
            continue
        unit = value[i : i + 2] if value[i : i + 2] in ("ms", "us") else c
        total += round(float(number) * _UNITS[unit])
        number = ""
        i += len(unit)
    if number:
        total += round(float(number) * _UNITS["s"])
    return total


def normalise(field: Field, value: str | None) -> str | None:
    """A value in a form two equal values share. Anything unparseable compares as written, so it shows as drift."""
    if value is None:
        return None
    try:
        match field.kind:
            case "bool":
                return _BOOLS.get(value, value)
            case "int":
                return str(int(value))
            case "list":
                # An empty list reads back as "": the same as unset.
                return ",".join(sorted({v.strip() for v in value.split(",") if v.strip()})) or None
            case "ip":
                return str(ip_interface(value) if "/" in value else ip_address(value))
            case "duration":
                return f"{_duration_us(value)}us"
            case "str":
                return value
    except ValueError, KeyError:
        return value
