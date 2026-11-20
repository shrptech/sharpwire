"""Rules that span the plugin's models and NetBox core objects, connected in SharpwireConfig.ready()."""

from typing import Any

from django.db.models.signals import m2m_changed, pre_save
from django.utils.translation import gettext as _
from ipam.models import Prefix
from utilities.exceptions import AbortRequest

from .choices import VNetStatusChoices
from .models import VNet, VNetSubnet


def keep_subnets_in_zone_vrf(sender: type, instance: Prefix, raw: bool = False, **kwargs: Any) -> None:
    """A VNet's subnet can't be moved out of its zone's VRF by editing the prefix."""
    if raw or instance.pk is None:
        return
    link = VNetSubnet.objects.filter(prefix_id=instance.pk).select_related("vnet__zone__vrf").first()
    if link is not None and instance.vrf_id != link.vnet.zone.vrf_id:
        raise AbortRequest(
            _("{prefix} is a subnet of {vnet}, so it must stay in VRF {vrf}. Remove it from the VNet first.").format(
                prefix=instance.prefix, vnet=link.vnet, vrf=link.vnet.zone.vrf
            )
        )


def replan_on_subnet_change(
    sender: type, instance: VNet | Prefix, action: str, reverse: bool, pk_set: set[int] | None, **kwargs: Any
) -> None:
    """An active VNet whose subnets change no longer matches what sharpwire applied."""
    if action not in ("post_add", "post_remove", "pre_clear"):
        return
    if not reverse:
        vnet_ids = {instance.pk}
        if instance.status == VNetStatusChoices.ACTIVE:
            instance.status = VNetStatusChoices.PLANNED
    elif action == "pre_clear":
        vnet_ids = set(instance.sharpwire_vnets.values_list("pk", flat=True))
    else:
        vnet_ids = pk_set or set()
    VNet.objects.filter(pk__in=vnet_ids, status=VNetStatusChoices.ACTIVE).update(status=VNetStatusChoices.PLANNED)


def connect() -> None:
    pre_save.connect(keep_subnets_in_zone_vrf, sender=Prefix, dispatch_uid="sharpwire_netbox_subnet_vrf")
    m2m_changed.connect(replan_on_subnet_change, sender=VNetSubnet, dispatch_uid="sharpwire_netbox_subnet_replan")
