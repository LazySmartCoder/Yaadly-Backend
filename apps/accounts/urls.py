from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from .views import DeleteAccountView, GoogleLoginView, GoogleProfileView

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("google/", GoogleLoginView.as_view(), name="google_login"),
    path("google/profile/", GoogleProfileView.as_view(), name="google_profile"),
    path("account/", DeleteAccountView.as_view(), name="delete_account"),
]
