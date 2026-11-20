"""NetBox's test helpers assume a core app's URL namespaces. These point them at the plugin's."""


class PluginAPITestMixin:
    view_namespace = "plugins-api:sharpwire_netbox"


class PluginViewTestMixin:
    def _get_base_url(self) -> str:
        return f"plugins:sharpwire_netbox:{self.model._meta.model_name}_{{}}"  # type: ignore[attr-defined]
