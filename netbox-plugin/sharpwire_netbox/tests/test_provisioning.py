from unittest import mock

from dcim.models import DeviceRole, Platform
from django.test import TestCase
from extras.choices import CustomFieldTypeChoices
from extras.models import CustomField, Tag
from ipam.models import Role

from sharpwire_netbox import provisioning


class ProvisioningTest(TestCase):
    """The test database is migrated, so post_migrate has already provisioned it once."""

    def assert_provisioned(self) -> None:
        for spec in provisioning.CUSTOM_FIELDS:
            cf = CustomField.objects.get(name=spec.name)
            self.assertEqual(cf.type, spec.type)
            labels = {f"{ct.app_label}.{ct.model}" for ct in cf.object_types.all()}
            self.assertTrue(set(spec.object_types) <= labels, spec.name)
        self.assertEqual(
            CustomField.objects.get(name="asn").related_object_type.model,
            "asn",
        )
        for slug, _ in provisioning.IPAM_ROLES:
            Role.objects.get(slug=slug)
        for slug, _, _ in provisioning.DEVICE_ROLES:
            DeviceRole.objects.get(slug=slug)
        for slug, _ in provisioning.PLATFORMS:
            Platform.objects.get(slug=slug)
        for slug, _ in provisioning.TAGS:
            Tag.objects.get(slug=slug)

    def test_migrate_provisions_everything(self) -> None:
        self.assert_provisioned()

    def test_provisioning_is_idempotent(self) -> None:
        counts = (CustomField.objects.count(), Role.objects.count(), Tag.objects.count())
        provisioning.provision()
        self.assertEqual((CustomField.objects.count(), Role.objects.count(), Tag.objects.count()), counts)

    def test_recreates_what_was_deleted(self) -> None:
        CustomField.objects.get(name="router_id").delete()
        Role.objects.get(slug="vtep").delete()
        provisioning.provision()
        self.assert_provisioned()

    def test_keeps_admin_changes(self) -> None:
        role = Role.objects.get(slug="mgmt")
        role.name = "Management"
        role.save()
        provisioning.provision()
        self.assertEqual(Role.objects.get(slug="mgmt").name, "Management")

    def test_adds_missing_object_types(self) -> None:
        cf = CustomField.objects.get(name="l2mtu")
        cf.object_types.clear()
        provisioning.provision()
        self.assertEqual([ct.model for ct in cf.object_types.all()], ["interface"])

    def test_leaves_a_custom_field_of_another_type_alone(self) -> None:
        CustomField.objects.filter(name="mlag_id").update(type=CustomFieldTypeChoices.TYPE_TEXT)
        with self.assertLogs("sharpwire_netbox.provisioning", "WARNING"):
            provisioning.provision()
        self.assertEqual(CustomField.objects.get(name="mlag_id").type, CustomFieldTypeChoices.TYPE_TEXT)

    def test_leaves_an_object_field_pointing_elsewhere_alone(self) -> None:
        from django.contrib.contenttypes.models import ContentType

        vrf_type = ContentType.objects.get_by_natural_key("ipam", "vrf")
        CustomField.objects.filter(name="asn").update(related_object_type=vrf_type)
        with self.assertLogs("sharpwire_netbox.provisioning", "WARNING"):
            provisioning.provision()
        self.assertEqual(CustomField.objects.get(name="asn").related_object_type, vrf_type)

    def test_never_raises(self) -> None:
        with (
            mock.patch.object(provisioning, "_ensure_custom_field", side_effect=RuntimeError("schema mismatch")),
            self.assertLogs("sharpwire_netbox.provisioning", "WARNING") as logs,
        ):
            provisioning.provision()
        self.assertEqual(len(logs.records), len(provisioning.CUSTOM_FIELDS))

    def test_name_taken_by_another_slug_is_logged_not_raised(self) -> None:
        Role.objects.get(slug="underlay").delete()
        Role.objects.create(name="Underlay", slug="underlay-old")
        with self.assertLogs("sharpwire_netbox.provisioning", "WARNING"):
            provisioning.provision()
        self.assertFalse(Role.objects.filter(slug="underlay").exists())
