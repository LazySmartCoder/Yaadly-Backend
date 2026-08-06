from django.contrib import admin

from .models import Gallery, JournalEntry


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "date", "mood", "type", "created_at"]
    list_filter = ["type", "mood", "date"]
    search_fields = ["title", "content"]
    date_hierarchy = "date"


@admin.register(Gallery)
class GalleryAdmin(admin.ModelAdmin):
    list_display = ["id", "entry", "url", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["entry__title", "url"]
