from django.urls import path

from .views import RegisterDeviceTokenView

urlpatterns = [
    path("push/token/", RegisterDeviceTokenView.as_view(), name="register_device_token"),
]
