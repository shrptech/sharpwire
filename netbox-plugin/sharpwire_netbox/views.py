from django.utils.translation import gettext_lazy as _
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import AddObject, BulkExport
from netbox.ui import attrs, layout, panels
from netbox.views import generic
from utilities.query import count_related
from utilities.views import register_model_view

from . import filtersets, forms, tables
from .models import VNet, Zone

#
# Panels
#


class ZonePanel(panels.ObjectAttributesPanel):
    kind = attrs.ChoiceAttr("kind")
    vrf = attrs.RelatedObjectAttr("vrf", linkify=True, label=_("VRF"))
    l3vni = attrs.NumericAttr("l3vni", label=_("L3VNI"))
    mtu = attrs.NumericAttr("mtu", label=_("MTU"))
    description = attrs.TextAttr("description")


class VNetPanel(panels.ObjectAttributesPanel):
    name = attrs.TextAttr("name", style="font-monospace")
    zone = attrs.RelatedObjectAttr("zone", linkify=True)
    vni = attrs.NumericAttr("vni", label=_("VNI"))
    status = attrs.ChoiceAttr("status")
    subnets = attrs.RelatedObjectListAttr("subnets", linkify=True)
    description = attrs.TextAttr("description")


#
# Zones
#


@register_model_view(Zone, "list", path="", detail=False)
class ZoneListView(generic.ObjectListView):
    # v1 has a single zone, so there are no bulk actions.
    actions = (AddObject, BulkExport)
    queryset = Zone.objects.annotate(vnet_count=count_related(VNet, "zone"))
    filterset = filtersets.ZoneFilterSet
    filterset_form = forms.ZoneFilterForm
    table = tables.ZoneTable


@register_model_view(Zone)
class ZoneView(generic.ObjectView):
    queryset = Zone.objects.all()
    template_name = "generic/object.html"
    layout = layout.Layout(
        layout.Row(
            layout.Column(ZonePanel(), TagsPanel(), panels.PluginContentPanel("left_page")),
            layout.Column(CustomFieldsPanel(), panels.CommentsPanel(), panels.PluginContentPanel("right_page")),
        ),
        layout.Row(
            layout.Column(
                panels.ObjectsTablePanel(
                    "sharpwire_netbox.vnet",
                    filters={"zone_id": lambda ctx: ctx["object"].pk},
                    title=_("VNets"),
                ),
                panels.PluginContentPanel("full_width_page"),
            ),
        ),
    )


@register_model_view(Zone, "add", detail=False)
@register_model_view(Zone, "edit")
class ZoneEditView(generic.ObjectEditView):
    queryset = Zone.objects.all()
    form = forms.ZoneForm


@register_model_view(Zone, "delete")
class ZoneDeleteView(generic.ObjectDeleteView):
    queryset = Zone.objects.all()


#
# VNets
#


@register_model_view(VNet, "list", path="", detail=False)
class VNetListView(generic.ObjectListView):
    queryset = VNet.objects.prefetch_related("subnets")
    filterset = filtersets.VNetFilterSet
    filterset_form = forms.VNetFilterForm
    table = tables.VNetTable


@register_model_view(VNet)
class VNetView(generic.ObjectView):
    queryset = VNet.objects.all()
    template_name = "generic/object.html"
    layout = layout.SimpleLayout(
        left_panels=[VNetPanel(), TagsPanel()],
        right_panels=[CustomFieldsPanel(), panels.CommentsPanel()],
    )


@register_model_view(VNet, "add", detail=False)
@register_model_view(VNet, "edit")
class VNetEditView(generic.ObjectEditView):
    queryset = VNet.objects.all()
    form = forms.VNetForm


@register_model_view(VNet, "delete")
class VNetDeleteView(generic.ObjectDeleteView):
    queryset = VNet.objects.all()


@register_model_view(VNet, "bulk_import", path="import", detail=False)
class VNetBulkImportView(generic.BulkImportView):
    queryset = VNet.objects.all()
    model_form = forms.VNetImportForm


@register_model_view(VNet, "bulk_edit", path="edit", detail=False)
class VNetBulkEditView(generic.BulkEditView):
    queryset = VNet.objects.all()
    filterset = filtersets.VNetFilterSet
    table = tables.VNetTable
    form = forms.VNetBulkEditForm


@register_model_view(VNet, "bulk_delete", path="delete", detail=False)
class VNetBulkDeleteView(generic.BulkDeleteView):
    queryset = VNet.objects.all()
    filterset = filtersets.VNetFilterSet
    table = tables.VNetTable
