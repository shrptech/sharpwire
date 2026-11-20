import json

from django.test import override_settings
from django.urls import reverse
from ipam.models import VRF, Prefix
from utilities.testing import APITestCase

from sharpwire_netbox.models import VNet, Zone


class GraphQLTest(APITestCase):
    """The shape the intent builder reads: VNets with their zone's VRF and L3VNI, and their subnets."""

    @classmethod
    def setUpTestData(cls) -> None:
        vrf = VRF.objects.create(name="vrf_public")
        zone = Zone.objects.create(kind="public", vrf=vrf, l3vni=20000, mtu=1500)
        vnet = VNet.objects.create(zone=zone, vni=200, status="active")
        vnet.subnets.set(
            [
                Prefix.objects.create(prefix="198.51.100.0/26", vrf=vrf),
                Prefix.objects.create(prefix="2001:db8:70:200::/64", vrf=vrf),
            ]
        )
        # Changing subnets re-plans an active VNet, so mark it active afterwards.
        VNet.objects.filter(pk=vnet.pk).update(status="active")
        VNet.objects.create(zone=zone, vni=201)

    @override_settings(LOGIN_REQUIRED=True)
    def test_vnet_list(self) -> None:
        self.add_permissions(
            "sharpwire_netbox.view_vnet", "sharpwire_netbox.view_zone", "ipam.view_prefix", "ipam.view_vrf"
        )
        query = """
        {
          vnet_list(filters: {status: {exact: STATUS_ACTIVE}}) {
            name vni status
            zone { kind l3vni mtu vrf { name } }
            subnets { prefix vrf { name } }
          }
        }
        """
        response = self.client.post(reverse("graphql"), data={"query": query}, format="json", **self.header)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(
            data["data"]["vnet_list"],
            [
                {
                    "name": "vnet200",
                    "vni": 200,
                    "status": "active",
                    "zone": {"kind": "public", "l3vni": 20000, "mtu": 1500, "vrf": {"name": "vrf_public"}},
                    "subnets": [
                        {"prefix": "198.51.100.0/26", "vrf": {"name": "vrf_public"}},
                        {"prefix": "2001:db8:70:200::/64", "vrf": {"name": "vrf_public"}},
                    ],
                }
            ],
        )
