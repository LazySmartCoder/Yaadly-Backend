from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.push.views import admin_shoot


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    # Registered before admin.site.urls so "admin/shoot/" is never swallowed
    # by the admin app-index pattern.
    path("admin/shoot/", admin_shoot, name="admin_shoot"),
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/", include("apps.journals.urls")),
    path("api/", include("apps.push.urls")),
]
