"""GraphQL: `zone`/`zone_list` and `vnet`/`vnet_list`. The intent builder reads the plugin's objects through these."""

import strawberry
import strawberry_django

from .types import VNetType, ZoneType


@strawberry.type(name="Query")
class SharpwireQuery:
    zone: ZoneType = strawberry_django.field()
    zone_list: list[ZoneType] = strawberry_django.field()

    vnet: VNetType = strawberry_django.field()
    vnet_list: list[VNetType] = strawberry_django.field()


schema = [SharpwireQuery]
