"""The two plugin models (docs/02-intent-model.md#netbox-mapping).

The checks here are only those NetBox can make on its own objects: shapes, uniqueness and VRF membership. The full
cross-target validation (collisions with live state, the public ranges, MTU) is sharpwire's, at plan time.
"""

from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from ipam.models import Prefix
from netbox.models import PrimaryModel

from .choices import VNetStatusChoices, ZoneKindChoices

# VXLAN carries a 24-bit VNI.
VNI_MIN = 1
VNI_MAX = 2**24 - 1

# A VNet is named `vnet<VNI>` (docs/02-intent-model.md), and PVE caps SDN VNet IDs at 8 characters.
VNET_VNI_MAX = 9999


class Zone(PrimaryModel):
    """An EVPN zone: one VRF and one L3VNI. In v1 the only zone is `public`, and its PVE zone ID is its kind."""

    kind = models.CharField(
        verbose_name=_("kind"),
        max_length=30,
        choices=ZoneKindChoices,
    )
    vrf = models.OneToOneField(
        to="ipam.VRF",
        on_delete=models.PROTECT,
        related_name="sharpwire_zone",
        verbose_name=_("VRF"),
    )
    l3vni = models.PositiveIntegerField(
        verbose_name=_("L3VNI"),
        unique=True,
        validators=[MinValueValidator(VNI_MIN), MaxValueValidator(VNI_MAX)],
        help_text=_("The zone's VRF VXLAN ID (vrf-vxlan in PVE)"),
    )
    mtu = models.PositiveIntegerField(
        verbose_name=_("MTU"),
        validators=[MinValueValidator(1280), MaxValueValidator(65535)],
        help_text=_("The MTU of the zone's VNets"),
    )

    clone_fields = ("vrf", "mtu")

    class Meta:
        ordering = ("kind",)
        verbose_name = _("zone")
        verbose_name_plural = _("zones")
        # v1 has one zone per kind, and the kind is the PVE zone ID. A constraint rather than unique=True, because
        # NetBox's API validates constraints but leaves field uniqueness to each serializer.
        constraints = (
            models.UniqueConstraint(
                fields=("kind",),
                name="%(app_label)s_%(class)s_unique_kind",
                violation_error_message=_("There is already a zone of this kind."),
            ),
        )

    def __str__(self) -> str:
        return self.kind

    def get_kind_color(self) -> str | None:
        return ZoneKindChoices.colors.get(self.kind)

    def clean(self) -> None:
        super().clean()
        if self.l3vni is not None and VNet.objects.filter(vni=self.l3vni).exists():
            raise ValidationError({"l3vni": _("A VNet already uses VNI {vni}.").format(vni=self.l3vni)})
        if self.pk is not None and self.vrf_id is not None:
            outside = Prefix.objects.filter(sharpwire_vnets__zone=self).exclude(vrf_id=self.vrf_id)
            if outside.exists():
                raise ValidationError(
                    {
                        "vrf": _("The zone's VNets have subnets outside this VRF: {prefixes}.").format(
                            prefixes=", ".join(str(p.prefix) for p in outside)
                        )
                    }
                )


class VNet(PrimaryModel):
    """An SDN VNet, named `vnet<VNI>`. Its VNI is also its VXLAN tag."""

    zone = models.ForeignKey(
        to=Zone,
        on_delete=models.PROTECT,
        related_name="vnets",
        verbose_name=_("zone"),
    )
    vni = models.PositiveIntegerField(
        verbose_name=_("VNI"),
        unique=True,
        validators=[MinValueValidator(VNI_MIN), MaxValueValidator(VNET_VNI_MAX)],
        help_text=_("The VXLAN tag. The VNet is named vnet<VNI>, which must fit PVE's 8-character VNet ID."),
    )
    subnets = models.ManyToManyField(
        to="ipam.Prefix",
        through="VNetSubnet",
        related_name="sharpwire_vnets",
        blank=True,
        verbose_name=_("subnets"),
        help_text=_("Prefixes in the zone's VRF. Each prefix's gateway is an anycast IP address inside it."),
    )
    status = models.CharField(
        verbose_name=_("status"),
        max_length=50,
        choices=VNetStatusChoices,
        default=VNetStatusChoices.PLANNED,
        help_text=_("Set by sharpwire once the VNet is applied. Back to planned when its VNI, zone or subnets change."),
    )

    clone_fields = ("zone",)

    class Meta:
        ordering = ("vni",)
        verbose_name = _("VNet")
        verbose_name_plural = _("VNets")

    def __str__(self) -> str:
        return self.name

    @property
    def name(self) -> str:
        return f"vnet{self.vni}"

    def get_status_color(self) -> str | None:
        return VNetStatusChoices.colors.get(self.status)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # An active VNet whose identity changes no longer matches what sharpwire applied (subnets: signals.py).
        if self.pk is not None and self.status == VNetStatusChoices.ACTIVE:
            applied = VNet.objects.filter(pk=self.pk).values_list("vni", "zone_id").first()
            if applied is not None and applied != (self.vni, self.zone_id):
                self.status = VNetStatusChoices.PLANNED
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if self.vni is not None and Zone.objects.filter(l3vni=self.vni).exists():
            raise ValidationError({"vni": _("A zone already uses VNI {vni} as its L3VNI.").format(vni=self.vni)})


class VNetSubnet(models.Model):
    """Links a VNet to one of its subnets. A prefix belongs to at most one VNet, and can't be deleted while it does."""

    vnet = models.ForeignKey(to=VNet, on_delete=models.CASCADE, related_name="+")
    prefix = models.OneToOneField(to="ipam.Prefix", on_delete=models.PROTECT, related_name="sharpwire_vnet_subnet")

    # Not a user-facing object type: hidden from custom fields, event rules and the like.
    _netbox_private = True

    class Meta:
        verbose_name = _("VNet subnet")
        verbose_name_plural = _("VNet subnets")

    def __str__(self) -> str:
        return f"{self.vnet}: {self.prefix}"

    def get_absolute_url(self) -> str:
        return self.vnet.get_absolute_url()


def subnet_errors(zone: Zone, subnets: Iterable[Prefix], vnet: VNet | None = None) -> list[str]:
    """Why these prefixes can't be the subnets of `vnet` in `zone`, if they can't.

    Used by the form and the API serializer: a model's clean() never sees the many-to-many values being saved.
    """
    subnets = list(subnets)
    errors = [
        _("{prefix} is not in VRF {vrf}, the zone's VRF.").format(prefix=p.prefix, vrf=zone.vrf)
        for p in subnets
        if p.vrf_id != zone.vrf_id
    ]
    taken = VNet.objects.filter(subnets__in=subnets).distinct()
    if vnet is not None and vnet.pk is not None:
        taken = taken.exclude(pk=vnet.pk)
    for other in taken.prefetch_related("subnets"):
        for p in other.subnets.all():
            if p in subnets:
                errors.append(_("{prefix} already belongs to {vnet}.").format(prefix=p.prefix, vnet=other))
    return errors
