from typing import Any

from ipam.api.serializers import PrefixSerializer, VRFSerializer
from ipam.models import Prefix
from netbox.api.fields import ChoiceField, RelatedObjectCountField, SerializedPKRelatedField
from netbox.api.serializers import PrimaryModelSerializer
from rest_framework import serializers

from ..choices import VNetStatusChoices, ZoneKindChoices
from ..models import VNet, Zone, subnet_errors


class ZoneSerializer(PrimaryModelSerializer):
    kind = ChoiceField(choices=ZoneKindChoices)
    vrf = VRFSerializer(nested=True)
    vnet_count = RelatedObjectCountField("vnets")

    class Meta:
        model = Zone
        fields = (
            "id", "url", "display_url", "display", "kind", "vrf", "l3vni", "mtu", "description", "owner", "comments",
            "tags", "custom_fields", "created", "last_updated", "vnet_count",
        )  # fmt: skip
        brief_fields = ("id", "url", "display", "kind", "l3vni", "description")


class VNetSerializer(PrimaryModelSerializer):
    zone = ZoneSerializer(nested=True)
    name = serializers.CharField(read_only=True)
    status = ChoiceField(choices=VNetStatusChoices, required=False)
    subnets = SerializedPKRelatedField(
        queryset=Prefix.objects.all(),
        serializer=PrefixSerializer,
        nested=True,
        required=False,
        many=True,
    )

    class Meta:
        model = VNet
        fields = (
            "id", "url", "display_url", "display", "name", "zone", "vni", "status", "subnets", "description", "owner",
            "comments", "tags", "custom_fields", "created", "last_updated",
        )  # fmt: skip
        brief_fields = ("id", "url", "display", "name", "vni", "status", "description")

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.nested:
            return super().validate(data)
        # Checked before the model's own validation, which can't see the many-to-many subnets.
        zone = data.get("zone", self.instance.zone if self.instance else None)
        subnets = data.get("subnets")
        if subnets is None and self.instance is not None:
            subnets = list(self.instance.subnets.all())
        if zone is not None and subnets:
            errors = subnet_errors(zone, subnets, self.instance)
            if errors:
                raise serializers.ValidationError({"subnets": errors})
        return super().validate(data)
