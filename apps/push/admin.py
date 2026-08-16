from django.contrib import admin

from .models import DeviceToken


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "platform", "is_active", "created_at", "updated_at"]
    list_filter = ["platform", "is_active", "created_at"]
    search_fields = ["token", "user__email", "user__username"]
    raw_id_fields = ["user"]
