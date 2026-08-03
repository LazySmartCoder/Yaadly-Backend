from django.contrib import admin

from .models import UserProfile

admin.site.site_header = "Yaadly Administration"
admin.site.site_title = "Yaadly"
admin.site.index_title = "Welcome to Yaadly Admin Panel"


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "display_name", "google_id", "phone_number", "gender", "created_at"]
    search_fields = ["user__email", "user__username", "display_name", "google_id"]
    readonly_fields = ["created_at", "updated_at"]
