from typing import TYPE_CHECKING, Annotated

import strawberry
from netbox.graphql.types import PrimaryObjectType, register_type

from .. import models
from .filters import VNetFilter, ZoneFilter

if TYPE_CHECKING:
    from ipam.graphql.types import PrefixType, VRFType


@register_type(models.Zone, fields="__all__", filters=ZoneFilter, pagination=True)
class ZoneType(PrimaryObjectType):
    vrf: Annotated["VRFType", strawberry.lazy("ipam.graphql.types")]
    vnets: list[Annotated["VNetType", strawberry.lazy("sharpwire_netbox.graphql.types")]]


@register_type(models.VNet, fields="__all__", filters=VNetFilter, pagination=True)
class VNetType(PrimaryObjectType):
    zone: Annotated["ZoneType", strawberry.lazy("sharpwire_netbox.graphql.types")]
    subnets: list[Annotated["PrefixType", strawberry.lazy("ipam.graphql.types")]]

    @strawberry.field
    def name(self) -> str:
        return self.name
