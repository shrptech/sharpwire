from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenuButton, PluginMenuItem


def _item(model: str, label: str, *, importable: bool = True) -> PluginMenuItem:
    buttons = [
        PluginMenuButton(
            link=f"plugins:sharpwire_netbox:{model}_add",
            title=_("Add"),
            icon_class="mdi mdi-plus-thick",
            permissions=[f"sharpwire_netbox.add_{model}"],
        )
    ]
    if importable:
        buttons.append(
            PluginMenuButton(
                link=f"plugins:sharpwire_netbox:{model}_bulk_import",
                title=_("Import"),
                icon_class="mdi mdi-upload",
                permissions=[f"sharpwire_netbox.add_{model}"],
            )
        )
    return PluginMenuItem(
        link=f"plugins:sharpwire_netbox:{model}_list",
        link_text=label,
        permissions=[f"sharpwire_netbox.view_{model}"],
        buttons=tuple(buttons),
    )


menu_items = (
    _item("zone", _("Zones"), importable=False),
    _item("vnet", _("VNets")),
)
