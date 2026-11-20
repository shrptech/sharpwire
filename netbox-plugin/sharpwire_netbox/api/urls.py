from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register("zones", views.ZoneViewSet)
router.register("vnets", views.VNetViewSet)

urlpatterns = router.urls
