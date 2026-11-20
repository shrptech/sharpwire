from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401 registers the views get_model_urls() finds

urlpatterns = [
    path("zones/", include(get_model_urls("sharpwire_netbox", "zone", detail=False))),
    path("zones/<int:pk>/", include(get_model_urls("sharpwire_netbox", "zone"))),
    path("vnets/", include(get_model_urls("sharpwire_netbox", "vnet", detail=False))),
    path("vnets/<int:pk>/", include(get_model_urls("sharpwire_netbox", "vnet"))),
]
