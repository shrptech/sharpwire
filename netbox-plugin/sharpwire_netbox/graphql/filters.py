from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.filters import PrimaryModelFilter, register_filter
from strawberry.scalars import ID
from strawberry_django import BaseFilterLookup

from .. import models

if TYPE_CHECKING:
    from ipam.graphql.filters import PrefixFilter, VRFFilter
    from netbox.graphql.filter_lookups import IntegerLookup

    from .enums import SharpwireVNetStatusEnum, SharpwireZoneKindEnum


@register_filter(models.Zone, lookups=True)
class ZoneFilter(PrimaryModelFilter):
    kind: (
        BaseFilterLookup[Annotated["SharpwireZoneKindEnum", strawberry.lazy("sharpwire_netbox.graphql.enums")]] | None
    ) = strawberry_django.filter_field()
    vrf: Annotated["VRFFilter", strawberry.lazy("ipam.graphql.filters")] | None = strawberry_django.filter_field()
    vrf_id: ID | None = strawberry_django.filter_field()
    l3vni: Annotated["IntegerLookup", strawberry.lazy("netbox.graphql.filter_lookups")] | None = (
        strawberry_django.filter_field()
    )
    mtu: Annotated["IntegerLookup", strawberry.lazy("netbox.graphql.filter_lookups")] | None = (
        strawberry_django.filter_field()
    )


@register_filter(models.VNet, lookups=True)
class VNetFilter(PrimaryModelFilter):
    zone: Annotated["ZoneFilter", strawberry.lazy("sharpwire_netbox.graphql.filters")] | None = (
        strawberry_django.filter_field()
    )
    zone_id: ID | None = strawberry_django.filter_field()
    vni: Annotated["IntegerLookup", strawberry.lazy("netbox.graphql.filter_lookups")] | None = (
        strawberry_django.filter_field()
    )
    status: (
        BaseFilterLookup[Annotated["SharpwireVNetStatusEnum", strawberry.lazy("sharpwire_netbox.graphql.enums")]] | None
    ) = strawberry_django.filter_field()
    subnets: Annotated["PrefixFilter", strawberry.lazy("ipam.graphql.filters")] | None = (
        strawberry_django.filter_field()
    )
