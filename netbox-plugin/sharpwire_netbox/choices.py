from django.utils.translation import gettext_lazy as _
from utilities.choices import ChoiceSet


class ZoneKindChoices(ChoiceSet):
    """v1 has only the shared public zone. Customer zones (`cNN`) come in phase 2 (docs/later.md)."""

    PUBLIC = "public"

    CHOICES = [
        (PUBLIC, _("Public"), "blue"),
    ]


class VNetStatusChoices(ChoiceSet):
    """Written by sharpwire: `active` once the VNet is applied to the cluster. sharpstack uses only active VNets."""

    PLANNED = "planned"
    ACTIVE = "active"

    CHOICES = [
        (PLANNED, _("Planned"), "cyan"),
        (ACTIVE, _("Active"), "green"),
    ]
