import strawberry

from ..choices import VNetStatusChoices, ZoneKindChoices

# Prefixed, so they can't collide with another app's enum in NetBox's single schema.
SharpwireZoneKindEnum = strawberry.enum(ZoneKindChoices.as_enum(name="SharpwireZoneKindEnum", prefix="kind"))
SharpwireVNetStatusEnum = strawberry.enum(VNetStatusChoices.as_enum(name="SharpwireVNetStatusEnum", prefix="status"))
