"""UI forms.

A VNet's `status` is written by sharpwire only (docs/06-drift-and-approvals.md), so no UI form edits it. sharpwire
sets it through the REST API.
"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _
from ipam.models import VRF, Prefix
from netbox.forms import PrimaryModelBulkEditForm, PrimaryModelFilterSetForm, PrimaryModelForm, PrimaryModelImportForm
from utilities.forms.fields import (
    CSVModelChoiceField,
    CSVModelMultipleChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet

from .choices import VNetStatusChoices, ZoneKindChoices
from .models import VNet, Zone, subnet_errors

#
# Zones
#


class ZoneForm(PrimaryModelForm):
    vrf = DynamicModelChoiceField(label=_("VRF"), queryset=VRF.objects.all())

    fieldsets = (FieldSet("kind", "vrf", "l3vni", "mtu", "description", "tags", name=_("Zone")),)

    class Meta:
        model = Zone
        fields = ("kind", "vrf", "l3vni", "mtu", "description", "owner", "comments", "tags")


class ZoneFilterForm(PrimaryModelFilterSetForm):
    model = Zone
    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("kind", "vrf_id", "l3vni", "mtu", name=_("Zone")),
        FieldSet("owner_group_id", "owner_id", name=_("Ownership")),
    )
    kind = forms.MultipleChoiceField(label=_("Kind"), choices=ZoneKindChoices, required=False)
    vrf_id = DynamicModelMultipleChoiceField(label=_("VRF"), queryset=VRF.objects.all(), required=False)
    l3vni = forms.IntegerField(label=_("L3VNI"), required=False)
    mtu = forms.IntegerField(label=_("MTU"), required=False)
    tag = TagFilterField(model)


#
# VNets
#


class _SubnetsMixin:
    """Checks a VNet's subnets, which the model's clean() can't see (they're many-to-many).

    A CSV update drops the fields its columns leave out, so a missing zone or subnets field means "unchanged".
    """

    instance: VNet
    cleaned_data: dict[str, Any]
    fields: dict[str, Any]

    def clean(self) -> dict[str, Any]:
        super().clean()  # type: ignore[misc]
        existing = self.instance.pk is not None
        if "zone" in self.fields:
            zone = self.cleaned_data.get("zone")
        elif existing:
            zone = self.instance.zone
        else:
            zone = None
        if "subnets" in self.fields:
            subnets = self.cleaned_data.get("subnets")
        elif existing:
            subnets = list(self.instance.subnets.all())
        else:
            subnets = []
        if zone is not None and subnets:
            errors = subnet_errors(zone, subnets, self.instance)
            if errors:
                raise forms.ValidationError({"subnets": errors})
        return self.cleaned_data


class VNetForm(_SubnetsMixin, PrimaryModelForm):
    zone = DynamicModelChoiceField(label=_("Zone"), queryset=Zone.objects.all())
    subnets = DynamicModelMultipleChoiceField(
        label=_("Subnets"),
        queryset=Prefix.objects.all(),
        required=False,
        help_text=_("Prefixes in the zone's VRF"),
    )

    fieldsets = (
        FieldSet("zone", "vni", "description", "tags", name=_("VNet")),
        FieldSet("subnets", name=_("Subnets")),
    )

    class Meta:
        model = VNet
        fields = ("zone", "vni", "subnets", "description", "owner", "comments", "tags")


class VNetImportForm(_SubnetsMixin, PrimaryModelImportForm):
    zone = CSVModelChoiceField(label=_("Zone"), queryset=Zone.objects.all(), to_field_name="kind")
    subnets = CSVModelMultipleChoiceField(
        label=_("Subnets"),
        queryset=Prefix.objects.all(),
        required=False,
        to_field_name="prefix",
        help_text=_("Prefixes in the zone's VRF, comma-separated in quotes"),
    )

    class Meta:
        model = VNet
        fields = ("zone", "vni", "subnets", "description", "owner", "comments", "tags")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A CIDR can exist in several VRFs. Look subnets up in the zone's VRF only.
        if "subnets" not in self.fields:
            return
        zone = None
        kind = self.data.get("zone") if self.data else None
        if kind:
            zone = Zone.objects.filter(kind=kind).first()
        elif self.instance.pk is not None:
            zone = self.instance.zone
        if zone is not None:
            self.fields["subnets"].queryset = Prefix.objects.filter(vrf=zone.vrf)


class VNetBulkEditForm(PrimaryModelBulkEditForm):
    model = VNet
    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class VNetFilterForm(PrimaryModelFilterSetForm):
    model = VNet
    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("zone_id", "vni", "status", "subnet_id", name=_("VNet")),
        FieldSet("owner_group_id", "owner_id", name=_("Ownership")),
    )
    zone_id = DynamicModelMultipleChoiceField(label=_("Zone"), queryset=Zone.objects.all(), required=False)
    vni = forms.IntegerField(label=_("VNI"), required=False)
    status = forms.MultipleChoiceField(label=_("Status"), choices=VNetStatusChoices, required=False)
    subnet_id = DynamicModelMultipleChoiceField(label=_("Subnet"), queryset=Prefix.objects.all(), required=False)
    tag = TagFilterField(model)
