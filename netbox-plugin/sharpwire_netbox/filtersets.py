import contextlib

import django_filters
import netaddr
from django.db.models import Q, QuerySet
from django.utils.translation import gettext as _
from ipam.models import VRF, Prefix
from netbox.filtersets import PrimaryModelFilterSet
from utilities.filtersets import register_filterset

from .choices import VNetStatusChoices, ZoneKindChoices
from .models import VNet, Zone


@register_filterset
class ZoneFilterSet(PrimaryModelFilterSet):
    kind = django_filters.MultipleChoiceFilter(choices=ZoneKindChoices)
    vrf_id = django_filters.ModelMultipleChoiceFilter(queryset=VRF.objects.all(), label=_("VRF (ID)"))
    vrf = django_filters.ModelMultipleChoiceFilter(
        field_name="vrf__name",
        queryset=VRF.objects.all(),
        to_field_name="name",
        label=_("VRF (name)"),
    )

    class Meta:
        model = Zone
        fields = ("id", "kind", "l3vni", "mtu", "description")

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        if not value.strip():
            return queryset
        q = Q(kind__icontains=value) | Q(vrf__name__icontains=value) | Q(description__icontains=value)
        if value.isdigit():
            q |= Q(l3vni=int(value))
        return queryset.filter(q)


@register_filterset
class VNetFilterSet(PrimaryModelFilterSet):
    zone_id = django_filters.ModelMultipleChoiceFilter(queryset=Zone.objects.all(), label=_("Zone (ID)"))
    zone = django_filters.ModelMultipleChoiceFilter(
        field_name="zone__kind",
        queryset=Zone.objects.all(),
        to_field_name="kind",
        label=_("Zone (kind)"),
    )
    status = django_filters.MultipleChoiceFilter(choices=VNetStatusChoices)
    subnet_id = django_filters.ModelMultipleChoiceFilter(
        field_name="subnets",
        queryset=Prefix.objects.all(),
        label=_("Subnet (ID)"),
    )

    class Meta:
        model = VNet
        fields = ("id", "vni", "status", "description")

    def search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        value = value.strip()
        if not value:
            return queryset
        q = Q(description__icontains=value)
        number = value.removeprefix("vnet")
        if number.isdigit():
            q |= Q(vni=int(number))
        with contextlib.suppress(netaddr.AddrFormatError, ValueError):
            q |= Q(subnets__prefix__net_contains_or_equals=str(netaddr.IPNetwork(value).cidr))
        return queryset.filter(q).distinct()
