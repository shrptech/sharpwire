from netbox.api.viewsets import NetBoxModelViewSet

from .. import filtersets
from ..models import VNet, Zone
from . import serializers


class ZoneViewSet(NetBoxModelViewSet):
    queryset = Zone.objects.all()
    serializer_class = serializers.ZoneSerializer
    filterset_class = filtersets.ZoneFilterSet


class VNetViewSet(NetBoxModelViewSet):
    queryset = VNet.objects.prefetch_related("subnets")
    serializer_class = serializers.VNetSerializer
    filterset_class = filtersets.VNetFilterSet
