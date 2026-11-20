from netbox.search import SearchIndex, register_search

from .models import VNet, Zone


@register_search
class ZoneIndex(SearchIndex):
    model = Zone
    fields = (
        ("kind", 100),
        ("l3vni", 100),
        ("description", 500),
        ("comments", 5000),
    )
    display_attrs = ("vrf", "l3vni", "description")


@register_search
class VNetIndex(SearchIndex):
    model = VNet
    fields = (
        ("name", 100),
        ("vni", 100),
        ("description", 500),
        ("comments", 5000),
    )
    display_attrs = ("zone", "status", "description")
