from django.test import TestCase
from ipam.models import VRF, Prefix
from utilities.testing import ChangeLoggedFilterSetTestMixin

from sharpwire_netbox.filtersets import VNetFilterSet, ZoneFilterSet
from sharpwire_netbox.models import VNet, Zone


class ZoneFilterSetTest(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = Zone.objects.all()
    filterset = ZoneFilterSet

    # These compare two or more objects, and there can be only one public zone. test_missing_filters still runs.
    test_id = None
    test_created = None
    test_last_updated = None

    @classmethod
    def setUpTestData(cls) -> None:
        cls.vrf = VRF.objects.create(name="vrf_public")
        Zone.objects.create(kind="public", vrf=cls.vrf, l3vni=20000, mtu=1500, description="shared")

    def test_kind(self) -> None:
        self.assertEqual(self.filterset({"kind": ["public"]}, self.queryset).qs.count(), 1)

    def test_vrf(self) -> None:
        self.assertEqual(self.filterset({"vrf_id": [self.vrf.pk]}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"vrf": ["vrf_public"]}, self.queryset).qs.count(), 1)

    def test_l3vni(self) -> None:
        self.assertEqual(self.filterset({"l3vni": [20000]}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"l3vni": [20001]}, self.queryset).qs.count(), 0)

    def test_q(self) -> None:
        self.assertEqual(self.filterset({"q": "20000"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "vrf_pub"}, self.queryset).qs.count(), 1)


class VNetFilterSetTest(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = VNet.objects.all()
    filterset = VNetFilterSet

    @classmethod
    def setUpTestData(cls) -> None:
        vrf = VRF.objects.create(name="vrf_public")
        cls.zone = Zone.objects.create(kind="public", vrf=vrf, l3vni=20000, mtu=1500)
        cls.prefixes = [Prefix.objects.create(prefix=f"198.51.100.{n * 64}/26", vrf=vrf) for n in range(3)]
        vnets = [
            VNet.objects.create(zone=cls.zone, vni=200, status="active", description="first"),
            VNet.objects.create(zone=cls.zone, vni=201, status="active"),
            VNet.objects.create(zone=cls.zone, vni=202),
        ]
        for vnet, prefix in zip(vnets, cls.prefixes, strict=True):
            vnet.subnets.set([prefix])
        # Changing subnets re-plans an active VNet, so mark them active afterwards.
        VNet.objects.filter(vni__in=(200, 201)).update(status="active")

    def get_m2m_filter_name(self, field):  # type: ignore[no-untyped-def]
        return "subnet" if field.name == "subnets" else super().get_m2m_filter_name(field)

    def test_zone(self) -> None:
        self.assertEqual(self.filterset({"zone_id": [self.zone.pk]}, self.queryset).qs.count(), 3)
        self.assertEqual(self.filterset({"zone": ["public"]}, self.queryset).qs.count(), 3)

    def test_vni(self) -> None:
        self.assertEqual(self.filterset({"vni": [200, 201]}, self.queryset).qs.count(), 2)

    def test_status(self) -> None:
        self.assertEqual(self.filterset({"status": ["active"]}, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({"status": ["planned"]}, self.queryset).qs.count(), 1)

    def test_subnet(self) -> None:
        self.assertEqual(self.filterset({"subnet_id": [self.prefixes[0].pk]}, self.queryset).qs.count(), 1)

    def test_q(self) -> None:
        self.assertEqual(self.filterset({"q": "vnet201"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "202"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "198.51.100.64/26"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "198.51.100.70"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "first"}, self.queryset).qs.count(), 1)
