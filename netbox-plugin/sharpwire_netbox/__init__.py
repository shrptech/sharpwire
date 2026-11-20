"""sharpwire's NetBox plugin.

It adds what NetBox core can't hold for sharpwire (docs/02-intent-model.md): the SDN models Zone and VNet, and on
every migrate the custom fields, roles, platforms and tags the intent builder reads (`provisioning.py`).
"""

from netbox.plugins import PluginConfig

__version__ = "0.1.0"


class SharpwireConfig(PluginConfig):
    name = "sharpwire_netbox"
    verbose_name = "sharpwire"
    description = "SDN zones and VNets, and the custom fields, roles and tags sharpwire reads"
    version = __version__
    base_url = "sharpwire"
    # The migrations depend on 4.7.5's core migrations, and 4.7.5 is the release it's tested against (pyproject.toml).
    min_version = "4.7.5"
    max_version = "4.7.99"

    def ready(self) -> None:
        super().ready()
        from django.db.models.signals import post_migrate

        from . import signals
        from .provisioning import provision_on_migrate

        signals.connect()
        post_migrate.connect(provision_on_migrate, sender=self, dispatch_uid="sharpwire_netbox_provision")


config = SharpwireConfig
