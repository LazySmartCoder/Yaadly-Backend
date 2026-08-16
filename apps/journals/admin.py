from django.contrib import admin

from .models import ChatSession, Gallery, JournalEntry


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ["user", "date", "status", "journal_entry", "updated_at"]
    list_filter = ["status", "date"]
    search_fields = ["user__username", "journal_entry__title"]
    date_hierarchy = "date"
    readonly_fields = ["messages"]


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
