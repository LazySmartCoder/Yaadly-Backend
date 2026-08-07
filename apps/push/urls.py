from django.urls import path

from .views import DeactivateDeviceTokenView, RegisterDeviceTokenView

urlpatterns = [
    path("push/token/", RegisterDeviceTokenView.as_view(), name="register_device_token"),
    path(
        "push/token/deactivate/",
        DeactivateDeviceTokenView.as_view(),
        name="deactivate_device_token",
    ),
]
