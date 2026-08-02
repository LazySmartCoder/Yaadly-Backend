from django.contrib import admin

from .models import JournalEntry


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "date", "mood", "type", "created_at"]
    list_filter = ["type", "mood", "date"]
    search_fields = ["title", "content"]
    date_hierarchy = "date"
