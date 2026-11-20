from django.test import override_settings
from django.urls import reverse
from ipam.models import VRF, Prefix
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from utilities.testing import ViewTestCases, create_tags

from sharpwire_netbox.models import VNet, Zone
from sharpwire_netbox.search import VNetIndex
from sharpwire_netbox.tests import PluginViewTestMixin


# There can be only one public zone, and NetBox's generic suites need two or three objects of a model. Zone gets the
# generic cases that work with one, and hand-written ones for the rest.
class ZoneViewTest(PluginViewTestMixin, ViewTestCases.GetObjectChangelogViewTestCase):
    model = Zone

    @classmethod
    def setUpTestData(cls) -> None:
        cls.vrfs = [VRF.objects.create(name="vrf_public"), VRF.objects.create(name="vrf_spare")]
        cls.zone = Zone.objects.create(kind="public", vrf=cls.vrfs[0], l3vni=20000, mtu=1500)
        VNet.objects.create(zone=cls.zone, vni=200)

    def setUp(self) -> None:
        super().setUp()
        self.add_permissions(
            "sharpwire_netbox.view_zone",
            "sharpwire_netbox.add_zone",
            "sharpwire_netbox.change_zone",
            "sharpwire_netbox.delete_zone",
            "sharpwire_netbox.view_vnet",
            "ipam.view_vrf",
        )

    def test_list(self) -> None:
        response = self.client.get(self._get_url("list"))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "vrf_public")

    def test_detail_shows_its_vnets(self) -> None:
        response = self.client.get(self.zone.get_absolute_url())
        self.assertHttpStatus(response, 200)
        # The VNets panel loads its table over HTMX from the filtered VNet list.
        table_url = f"{reverse('plugins:sharpwire_netbox:vnet_list')}?embedded=True&zone_id={self.zone.pk}"
        self.assertContains(response, table_url)
        table = self.client.get(table_url, HTTP_HX_REQUEST="true")
        self.assertContains(table, "vnet200")

    def test_create(self) -> None:
        VNet.objects.all().delete()
        Zone.objects.all().delete()
        form = {"kind": "public", "vrf": self.vrfs[1].pk, "l3vni": 20001, "mtu": 1500}
        response = self.client.post(self._get_url("add"), form)
        self.assertHttpStatus(response, 302)
        self.assertEqual(Zone.objects.get().vrf, self.vrfs[1])

    def test_second_public_zone_is_rejected(self) -> None:
        form = {"kind": "public", "vrf": self.vrfs[1].pk, "l3vni": 20001, "mtu": 1500}
        response = self.client.post(self._get_url("add"), form)
        self.assertHttpStatus(response, 200)
        self.assertEqual(Zone.objects.count(), 1)

    def test_edit(self) -> None:
        form = {"kind": "public", "vrf": self.vrfs[0].pk, "l3vni": 20000, "mtu": 9000}
        response = self.client.post(self._get_url("edit", self.zone), form)
        self.assertHttpStatus(response, 302)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.mtu, 9000)

    def test_delete(self) -> None:
        VNet.objects.all().delete()
        response = self.client.post(self._get_url("delete", self.zone), {"confirm": True})
        self.assertHttpStatus(response, 302)
        self.assertFalse(Zone.objects.exists())


class VNetViewTest(PluginViewTestMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = VNet

    @classmethod
    def setUpTestData(cls) -> None:
        vrf = VRF.objects.create(name="vrf_public")
        zone = Zone.objects.create(kind="public", vrf=vrf, l3vni=20000, mtu=1500)
        prefixes = [Prefix.objects.create(prefix=f"198.51.100.{n * 64}/26", vrf=vrf) for n in range(4)]
        vnets = [VNet.objects.create(zone=zone, vni=vni) for vni in (200, 201, 202)]
        vnets[0].subnets.set([prefixes[0]])
        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "zone": zone.pk,
            "vni": 300,
            "subnets": [prefixes[3].pk],
            "description": "A new VNet",
            "tags": [t.pk for t in tags],
        }
        cls.csv_data = (
            "zone,vni,subnets,description",
            "public,203,198.51.100.64/26,Foo",
            "public,204,198.51.100.128/26,Bar",
            "public,205,,Baz",
        )
        cls.csv_update_data = (
            "id,description",
            f"{vnets[0].pk},New description 1",
            f"{vnets[1].pk},New description 2",
            f"{vnets[2].pk},New description 3",
        )
        cls.bulk_edit_data = {"description": "New description"}

    def _import(self, *rows: str) -> object:
        self.add_permissions("sharpwire_netbox.add_vnet", "sharpwire_netbox.change_vnet")
        data = {"format": ImportFormatChoices.CSV, "data": "\n".join(rows), "csv_delimiter": CSVDelimiterChoices.AUTO}
        return self.client.post(self._get_url("bulk_import"), data)

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_import_update_checks_subnets_without_a_zone_column(self) -> None:
        vnet200, vnet201 = VNet.objects.get(vni=200), VNet.objects.get(vni=201)
        Prefix.objects.create(prefix="203.0.113.0/24")
        # A prefix outside the zone's VRF: not found in the zone's VRF, so the import fails.
        self.assertHttpStatus(self._import("id,subnets", f"{vnet201.pk},203.0.113.0/24"), 200)
        # A prefix of another VNet.
        self.assertHttpStatus(self._import("id,subnets", f"{vnet201.pk},198.51.100.0/26"), 200)
        self.assertEqual(list(vnet201.subnets.all()), [])
        self.assertEqual([str(p.prefix) for p in vnet200.subnets.all()], ["198.51.100.0/26"])

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_import_finds_subnets_in_the_zone_vrf_when_a_cidr_is_in_several(self) -> None:
        Prefix.objects.create(prefix="198.51.100.192/26")  # the same CIDR in the global table
        self.assertHttpStatus(self._import("zone,vni,subnets", "public,220,198.51.100.192/26"), 302)
        [subnet] = VNet.objects.get(vni=220).subnets.all()
        self.assertEqual(subnet.vrf.name, "vrf_public")

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_editing_an_active_vnet_replans_it(self) -> None:
        vnet = VNet.objects.get(vni=200)
        VNet.objects.filter(pk=vnet.pk).update(status="active")
        self.add_permissions("sharpwire_netbox.change_vnet")
        form = {"zone": vnet.zone.pk, "vni": 777, "subnets": [p.pk for p in vnet.subnets.all()]}
        self.assertHttpStatus(self.client.post(self._get_url("edit", vnet), form), 302)
        vnet.refresh_from_db()
        self.assertEqual((vnet.name, vnet.status), ("vnet777", "planned"))

    def test_global_search_indexes_the_vnet_name(self) -> None:
        cached = {value.value for value in VNetIndex.to_cache(VNet.objects.get(vni=201))}
        self.assertIn("vnet201", cached)

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_form_rejects_a_subnet_outside_the_zone_vrf(self) -> None:
        self.add_permissions("sharpwire_netbox.add_vnet")
        outside = Prefix.objects.create(prefix="203.0.113.0/24")
        response = self.client.post(
            reverse("plugins:sharpwire_netbox:vnet_add"),
            {**self.form_data, "vni": 301, "subnets": [outside.pk]},
        )
        self.assertHttpStatus(response, 200)
        self.assertFalse(VNet.objects.filter(vni=301).exists())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_form_does_not_edit_status(self) -> None:
        self.add_permissions("sharpwire_netbox.add_vnet")
        response = self.client.post(
            reverse("plugins:sharpwire_netbox:vnet_add"),
            {**self.form_data, "vni": 302, "status": "active"},
        )
        self.assertHttpStatus(response, 302)
        self.assertEqual(VNet.objects.get(vni=302).status, "planned")
