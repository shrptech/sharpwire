from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from ipam.models import VRF, Prefix
from utilities.exceptions import AbortRequest

from sharpwire_netbox.models import VNet, VNetSubnet, Zone, subnet_errors


class ZoneVNetTest(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.vrf = VRF.objects.create(name="vrf_public")
        cls.other_vrf = VRF.objects.create(name="other")
        cls.zone = Zone.objects.create(kind="public", vrf=cls.vrf, l3vni=20000, mtu=1500)
        cls.p4 = Prefix.objects.create(prefix="198.51.100.0/26", vrf=cls.vrf)
        cls.p6 = Prefix.objects.create(prefix="2001:db8:70:200::/64", vrf=cls.vrf)
        cls.global_prefix = Prefix.objects.create(prefix="198.51.100.64/26")

    def test_vnet_is_named_after_its_vni(self) -> None:
        self.assertEqual(str(VNet(zone=self.zone, vni=200)), "vnet200")

    def test_only_one_public_zone(self) -> None:
        with self.assertRaises(ValidationError):
            Zone(kind="public", vrf=self.other_vrf, l3vni=20001, mtu=1500).full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Zone.objects.create(kind="public", vrf=self.other_vrf, l3vni=20001, mtu=1500)

    def test_vni_must_fit_the_pve_vnet_id(self) -> None:
        VNet(zone=self.zone, vni=9999).full_clean()
        with self.assertRaises(ValidationError) as cm:
            VNet(zone=self.zone, vni=10000).full_clean()
        self.assertIn("vni", cm.exception.message_dict)

    def test_vnet_vni_cannot_be_a_zone_l3vni(self) -> None:
        with self.assertRaises(ValidationError) as cm:
            VNet(zone=self.zone, vni=20000).full_clean()
        self.assertIn("vni", cm.exception.message_dict)

    def test_zone_l3vni_cannot_be_a_vnet_vni(self) -> None:
        VNet.objects.create(zone=self.zone, vni=300)
        self.zone.l3vni = 300
        with self.assertRaises(ValidationError) as cm:
            self.zone.full_clean()
        self.assertIn("l3vni", cm.exception.message_dict)

    def test_subnets_in_the_zone_vrf_pass(self) -> None:
        self.assertEqual(subnet_errors(self.zone, [self.p4, self.p6]), [])

    def test_subnet_outside_the_zone_vrf_fails(self) -> None:
        errors = subnet_errors(self.zone, [self.global_prefix])
        self.assertEqual(len(errors), 1)
        self.assertIn("198.51.100.64/26", errors[0])

    def test_subnet_in_another_vnet_fails(self) -> None:
        vnet = VNet.objects.create(zone=self.zone, vni=200)
        vnet.subnets.set([self.p4])
        other = VNet(zone=self.zone, vni=201)
        self.assertEqual(len(subnet_errors(self.zone, [self.p4], other)), 1)
        # Its own subnets don't count against it.
        self.assertEqual(subnet_errors(self.zone, [self.p4, self.p6], vnet), [])

    def test_zone_vrf_change_must_keep_its_subnets(self) -> None:
        vnet = VNet.objects.create(zone=self.zone, vni=200)
        vnet.subnets.set([self.p4])
        self.zone.vrf = self.other_vrf
        with self.assertRaises(ValidationError) as cm:
            self.zone.full_clean()
        self.assertIn("vrf", cm.exception.message_dict)

    def test_zone_with_vnets_cannot_be_deleted(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200)
        with self.assertRaises(ProtectedError):
            self.zone.delete()

    def test_a_prefix_belongs_to_one_vnet_in_the_database(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200).subnets.add(self.p4)
        other = VNet.objects.create(zone=self.zone, vni=201)
        with self.assertRaises(IntegrityError), transaction.atomic():
            other.subnets.add(self.p4)

    def test_a_subnet_cannot_be_deleted(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200).subnets.add(self.p4)
        with self.assertRaises(ProtectedError):
            self.p4.delete()

    def test_deleting_a_vnet_frees_its_subnets(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200).subnets.add(self.p4)
        VNet.objects.get(vni=200).delete()
        self.assertFalse(VNetSubnet.objects.exists())
        self.p4.delete()

    def test_a_subnet_cannot_leave_the_zone_vrf(self) -> None:
        VNet.objects.create(zone=self.zone, vni=200).subnets.add(self.p4)
        self.p4.vrf = None
        with self.assertRaises(AbortRequest):
            self.p4.save()
        # Other prefixes move freely.
        self.global_prefix.vrf = self.other_vrf
        self.global_prefix.save()


class ReplanTest(TestCase):
    """An active VNet whose VNI, zone or subnets change goes back to planned."""

    @classmethod
    def setUpTestData(cls) -> None:
        vrf = VRF.objects.create(name="vrf_public")
        cls.zone = Zone.objects.create(kind="public", vrf=vrf, l3vni=20000, mtu=1500)
        cls.p1 = Prefix.objects.create(prefix="198.51.100.0/26", vrf=vrf)
        cls.p2 = Prefix.objects.create(prefix="198.51.100.64/26", vrf=vrf)

    def setUp(self) -> None:
        self.vnet = VNet.objects.create(zone=self.zone, vni=200, status="active")
        self.vnet.subnets.set([self.p1])
        VNet.objects.filter(pk=self.vnet.pk).update(status="active")
        self.vnet.refresh_from_db()

    def status(self) -> str:
        return VNet.objects.get(pk=self.vnet.pk).status

    def test_vni_change(self) -> None:
        self.vnet.vni = 201
        self.vnet.save()
        self.assertEqual(self.status(), "planned")

    def test_other_edits_keep_it_active(self) -> None:
        self.vnet.description = "renamed"
        self.vnet.save()
        self.vnet.subnets.set([self.p1])  # unchanged
        self.assertEqual(self.status(), "active")

    def test_subnet_added(self) -> None:
        self.vnet.subnets.add(self.p2)
        self.assertEqual(self.status(), "planned")
        self.assertEqual(self.vnet.status, "planned")

    def test_subnet_removed(self) -> None:
        self.vnet.subnets.remove(self.p1)
        self.assertEqual(self.status(), "planned")

    def test_subnets_cleared_from_the_prefix_side(self) -> None:
        self.p1.sharpwire_vnets.clear()
        self.assertEqual(self.status(), "planned")
