import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import PrimaryModelTable, columns

from .models import VNet, Zone


class ZoneTable(PrimaryModelTable):
    kind = columns.ChoiceFieldColumn(verbose_name=_("Kind"), linkify=True)
    vrf = tables.Column(verbose_name=_("VRF"), linkify=True)
    l3vni = tables.Column(verbose_name=_("L3VNI"))
    mtu = tables.Column(verbose_name=_("MTU"))
    vnet_count = columns.LinkedCountColumn(
        viewname="plugins:sharpwire_netbox:vnet_list",
        url_params={"zone_id": "pk"},
        verbose_name=_("VNets"),
    )
    tags = columns.TagColumn(url_name="plugins:sharpwire_netbox:zone_list")

    class Meta(PrimaryModelTable.Meta):
        model = Zone
        fields = (
            "pk", "id", "kind", "vrf", "l3vni", "mtu", "vnet_count", "description", "comments", "tags", "created",
            "last_updated",
        )  # fmt: skip
        default_columns = ("pk", "kind", "vrf", "l3vni", "mtu", "vnet_count", "description")


class VNetTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True, order_by=("vni",))
    zone = tables.Column(verbose_name=_("Zone"), linkify=True)
    vni = tables.Column(verbose_name=_("VNI"))
    status = columns.ChoiceFieldColumn(verbose_name=_("Status"))
    subnets = columns.ManyToManyColumn(verbose_name=_("Subnets"), linkify_item=True)
    tags = columns.TagColumn(url_name="plugins:sharpwire_netbox:vnet_list")

    class Meta(PrimaryModelTable.Meta):
        model = VNet
        fields = (
            "pk", "id", "name", "zone", "vni", "status", "subnets", "description", "comments", "tags", "created",
            "last_updated",
        )  # fmt: skip
        default_columns = ("pk", "name", "zone", "vni", "status", "subnets", "description")
