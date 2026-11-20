"""The NetBox objects sharpwire's intent builder looks up by name or slug (docs/02-intent-model.md#netbox-mapping).

They're created after every `migrate`, so a fresh NetBox, a test database and an upgraded one all have them.
Provisioning only adds: it never renames, retypes or deletes what an admin has changed. Anything that can't be
created as declared (a name already taken, a custom field of another type) is logged and left alone, and sharpwire's
validation reports it.

Nothing here is a network value. Those all come from objects an admin creates.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.apps import AppConfig
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from extras.choices import CustomFieldTypeChoices

logger = logging.getLogger("sharpwire_netbox.provisioning")

GROUP = "sharpwire"

_IPV4 = r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"


@dataclass(frozen=True)
class CustomFieldSpec:
    name: str
    type: str
    object_types: tuple[str, ...]
    label: str
    description: str
    related_object_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


CUSTOM_FIELDS = (
    CustomFieldSpec(
        "sharpwire_managed",
        CustomFieldTypeChoices.TYPE_BOOLEAN,
        ("dcim.device",),
        "Managed by sharpwire",
        "sharpwire reads, diffs and applies this device's config",
    ),
    CustomFieldSpec(
        "asn",
        CustomFieldTypeChoices.TYPE_OBJECT,
        ("dcim.device",),
        "ASN",
        "The device's own BGP ASN. NetBox assigns ASNs to sites, so this field links the device to one",
        related_object_type="ipam.asn",
    ),
    CustomFieldSpec(
        "router_id",
        CustomFieldTypeChoices.TYPE_TEXT,
        ("dcim.device",),
        "Router ID",
        "BGP router ID, as a dotted quad",
        extra={"validation_regex": _IPV4},
    ),
    CustomFieldSpec(
        "l2mtu",
        CustomFieldTypeChoices.TYPE_INTEGER,
        ("dcim.interface",),
        "L2 MTU",
        "RouterOS l2mtu of a physical port: at least its bond's MTU + 4",
        extra={"validation_minimum": 1},
    ),
    CustomFieldSpec(
        "mlag_id",
        CustomFieldTypeChoices.TYPE_INTEGER,
        ("dcim.interface",),
        "MLAG ID",
        "Set on MLAG bonds only, with the same value on both leafs",
        extra={"validation_minimum": 0},
    ),
    CustomFieldSpec(
        "max_subnet_length",
        CustomFieldTypeChoices.TYPE_INTEGER,
        ("ipam.prefix",),
        "Max subnet length",
        "On public ranges: subnets from the range's length up to this length are accepted",
        extra={"validation_minimum": 0, "validation_maximum": 128},
    ),
    CustomFieldSpec(
        "firewall_enabled",
        CustomFieldTypeChoices.TYPE_BOOLEAN,
        ("virtualization.cluster",),
        "Firewall enabled",
        "The PVE cluster firewall is on",
    ),
)

# (slug, name). ipam.Role is shared by VLANs and prefixes.
IPAM_ROLES = (
    ("mgmt", "Mgmt"),
    ("underlay", "Underlay"),
    ("public-handoff", "Public handoff"),
    ("vtep", "VTEP"),
    ("public", "Public"),
)

# (slug, name, color)
DEVICE_ROLES = (
    ("core", "Core", "2196f3"),
    ("leaf", "Leaf", "4caf50"),
    ("pve-node", "PVE node", "ff9800"),
)

# The driver is chosen by platform.
PLATFORMS = (
    ("routeros", "RouterOS"),
    ("proxmox", "Proxmox VE"),
)

# overlay and public-handoff mark ASNs; sharpwire-managed marks the NetBox objects sharpwire owns.
TAGS = (
    ("overlay", "overlay"),
    ("public-handoff", "public-handoff"),
    ("sharpwire-managed", "sharpwire-managed"),
)


def provision(using: str = "default") -> None:
    """Create whatever's missing. Safe to run any number of times, and never raises: each failure is logged."""
    from dcim.models import DeviceRole, Platform
    from extras.models import CustomField, Tag
    from ipam.models import Role

    for spec in CUSTOM_FIELDS:
        _guarded(f"custom field {spec.name!r}", using, _ensure_custom_field, CustomField, spec, using)
    for model, items in (
        (Role, [(slug, {"name": name}) for slug, name in IPAM_ROLES]),
        (DeviceRole, [(slug, {"name": name, "color": color}) for slug, name, color in DEVICE_ROLES]),
        (Platform, [(slug, {"name": name}) for slug, name in PLATFORMS]),
        (Tag, [(slug, {"name": name}) for slug, name in TAGS]),
    ):
        for slug, defaults in items:
            _guarded(f"{model._meta.verbose_name} {slug!r}", using, _ensure, model, using, slug, defaults)


def provision_on_migrate(sender: AppConfig, using: str = "default", **kwargs: Any) -> None:
    # post_migrate also fires after a partial or backward migrate of core apps, when NetBox's tables may not match
    # its models. provision() never raises, so that can't fail the migrate.
    provision(using)


def _guarded(what: str, using: str, func: Callable[..., None], *args: Any) -> None:
    try:
        with transaction.atomic(using=using):
            func(*args)
    except Exception:
        logger.warning("Couldn't provision %s. Create or fix it by hand.", what, exc_info=True)


def _content_type(label: str, using: str) -> ContentType:
    app_label, model = label.split(".")
    return ContentType.objects.db_manager(using).get_by_natural_key(app_label, model)


def _ensure(model: type, using: str, slug: str, defaults: dict[str, Any]) -> None:
    if model.objects.using(using).filter(slug=slug).exists():
        return
    obj = model(slug=slug, **defaults)
    obj.full_clean()
    obj.save(using=using)


def _ensure_custom_field(model: type, spec: CustomFieldSpec, using: str) -> None:
    related = _content_type(spec.related_object_type, using) if spec.related_object_type else None
    cf = model.objects.using(using).filter(name=spec.name).first()
    if cf is None:
        cf = model(
            name=spec.name,
            type=spec.type,
            label=spec.label,
            description=spec.description,
            group_name=GROUP,
            related_object_type=related,
            **spec.extra,
        )
        cf.save(using=using)
    elif cf.type != spec.type or cf.related_object_type_id != (related.pk if related else None):
        logger.warning(
            "Custom field %r exists as %s, but sharpwire reads it as %s. Fix it by hand.",
            spec.name,
            _describe(cf.type, cf.related_object_type),
            _describe(spec.type, related),
        )
        return
    wanted = {_content_type(label, using) for label in spec.object_types}
    missing = wanted - set(cf.object_types.all())
    if missing:
        cf.object_types.add(*missing)


def _describe(type_: str, related: ContentType | None) -> str:
    return f"{type_} ({related.app_label}.{related.model})" if related else type_
