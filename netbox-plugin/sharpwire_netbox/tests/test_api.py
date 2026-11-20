from django.urls import reverse
from ipam.models import VRF, Prefix
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases

from sharpwire_netbox.models import VNet, Zone
from sharpwire_netbox.tests import PluginAPITestMixin


class AppTest(APITestCase):
    def test_root(self) -> None:
        response = self.client.get(reverse("plugins-api:sharpwire_netbox-api:api-root"), **self.header)
        self.assertEqual(response.status_code, 200)


# There can be only one public zone, and NetBox's generic suites need two or three objects of a model. Zone gets the
# generic cases that work with one, and hand-written ones for the rest.
class ZoneTest(PluginAPITestMixin, APIViewTestCases.GraphQLTestCase):
    model = Zone
    brief_fields = ["description", "display", "id", "kind", "l3vni", "url"]
    user_permissions = ("ipam.view_vrf",)

    @classmethod
    def setUpTestData(cls) -> None:
        cls.vrf = VRF.objects.create(name="vrf_public")
        cls.spare_vrf = VRF.objects.create(name="vrf_spare")
        cls.zone = Zone.objects.create(kind="public", vrf=cls.vrf, l3vni=20000, mtu=1500)

    def test_get(self) -> None:
        self.add_permissions("sharpwire_netbox.view_zone")
        response = self.client.get(self._get_detail_url(self.zone), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["display"], "public")

        response = self.client.get(f"{self._get_list_url()}?brief=1", **self.header)
        self.assertEqual(sorted(response.data["results"][0]), self.brief_fields)

    def test_list(self) -> None:
        self.add_permissions("sharpwire_netbox.view_zone")
        response = self.client.get(self._get_list_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        [zone] = response.data["results"]
        self.assertEqual(zone["kind"]["value"], "public")
        self.assertEqual(zone["vrf"]["name"], "vrf_public")
        self.assertEqual((zone["l3vni"], zone["mtu"], zone["vnet_count"]), (20000, 1500, 0))

    def test_create(self) -> None:
        Zone.objects.all().delete()
        self.add_permissions("sharpwire_netbox.add_zone")
        data = {"kind": "public", "vrf": self.vrf.pk, "l3vni": 20000, "mtu": 1500}
        response = self.client.post(self._get_list_url(), data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(Zone.objects.get().l3vni, 20000)

    def test_second_public_zone_is_rejected(self) -> None:
        self.add_permissions("sharpwire_netbox.add_zone")
        data = {"kind": "public", "vrf": self.spare_vrf.pk, "l3vni": 20001, "mtu": 1500}
        response = self.client.post(self._get_list_url(), data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_update(self) -> None:
        self.add_permissions("sharpwire_netbox.change_zone")
        url = self._get_detail_url(self.zone)
        response = self.client.patch(url, {"mtu": 9000}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.mtu, 9000)

        response = self.client.patch(url, {"mtu": 100}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_delete(self) -> None:
        self.add_permissions("sharpwire_netbox.delete_zone")
        response = self.client.delete(self._get_detail_url(self.zone), **self.header)
        self.assertHttpStatus(response, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Zone.objects.exists())

    def test_zone_with_vnets_cannot_be_deleted(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200)
        self.add_permissions("sharpwire_netbox.delete_zone")
        response = self.client.delete(self._get_detail_url(self.zone), **self.header)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertTrue(Zone.objects.exists())


class VNetTest(PluginAPITestMixin, APIViewTestCases.APIViewTestCase):
    model = VNet
    brief_fields = ["description", "display", "id", "name", "status", "url", "vni"]
    user_permissions = ("sharpwire_netbox.view_zone",)
    bulk_update_data = {"description": "New description"}
    bulk_update_invalid_data = {"vni": 0}

    @classmethod
    def setUpTestData(cls) -> None:
        vrf = VRF.objects.create(name="vrf_public")
        cls.zone = Zone.objects.create(kind="public", vrf=vrf, l3vni=20000, mtu=1500)
        cls.prefixes = [Prefix.objects.create(prefix=f"198.51.100.{n * 64}/26", vrf=vrf) for n in range(4)]
        cls.global_prefix = Prefix.objects.create(prefix="203.0.113.0/24")
        vnets = [VNet.objects.create(zone=cls.zone, vni=vni) for vni in (200, 201, 202)]
        vnets[0].subnets.set([cls.prefixes[0]])
        cls.create_data = [
            {"zone": cls.zone.pk, "vni": 203, "subnets": [cls.prefixes[1].pk]},
            {"zone": cls.zone.pk, "vni": 204, "subnets": [cls.prefixes[2].pk]},
            {"zone": cls.zone.pk, "vni": 205, "status": "active"},
        ]

    def _post(self, data: dict) -> object:
        self.add_permissions("sharpwire_netbox.add_vnet", "ipam.view_prefix")
        return self.client.post(self._get_list_url(), data, format="json", **self.header)

    def test_name_and_default_status(self) -> None:
        response = self._post({"zone": self.zone.pk, "vni": 210})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "vnet210")
        self.assertEqual(response.data["status"]["value"], "planned")

    def test_subnet_outside_the_zone_vrf_is_rejected(self) -> None:
        response = self._post({"zone": self.zone.pk, "vni": 210, "subnets": [self.global_prefix.pk]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnets", response.data)

    def test_subnet_of_another_vnet_is_rejected(self) -> None:
        response = self._post({"zone": self.zone.pk, "vni": 210, "subnets": [self.prefixes[0].pk]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subnets", response.data)

    def test_vni_clashing_with_the_l3vni_is_rejected(self) -> None:
        response = self._post({"zone": self.zone.pk, "vni": 20000})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_patching_subnets_is_checked(self) -> None:
        vnet = VNet.objects.get(vni=201)
        self.add_permissions("sharpwire_netbox.change_vnet", "ipam.view_prefix")
        url = self._get_detail_url(vnet)
        response = self.client.patch(url, {"subnets": [self.global_prefix.pk]}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        # Only the VNI changes: the existing subnets are still checked, and still pass.
        vnet.subnets.set([self.prefixes[3]])
        response = self.client.patch(url, {"vni": 299}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

    def test_changing_an_active_vnets_subnets_replans_it(self) -> None:
        vnet = VNet.objects.get(vni=200)
        VNet.objects.filter(pk=vnet.pk).update(status="active")
        self.add_permissions("sharpwire_netbox.change_vnet", "ipam.view_prefix")
        response = self.client.patch(
            self._get_detail_url(vnet), {"subnets": [self.prefixes[3].pk]}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["status"]["value"], "planned")

    def test_sharpwire_sets_status(self) -> None:
        vnet = VNet.objects.get(vni=200)
        self.add_permissions("sharpwire_netbox.change_vnet", "ipam.view_prefix")
        response = self.client.patch(self._get_detail_url(vnet), {"status": "active"}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        vnet.refresh_from_db()
        self.assertEqual(vnet.status, "active")
        # The PATCH left its subnets alone.
        self.assertEqual(list(vnet.subnets.all()), [self.prefixes[0]])
