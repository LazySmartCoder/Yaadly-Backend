import uuid

from django.conf import settings
from django.db import models


class JournalEntry(models.Model):
    class EntryType(models.TextChoices):
        TEXT = "text", "Text"
        VOICE = "voice", "Voice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="journal_entries",
    )
    date = models.DateField()
    title = models.CharField(max_length=255)
    content = models.TextField()
    mood = models.CharField(max_length=50, blank=True)
    type = models.CharField(max_length=10, choices=EntryType.choices, default=EntryType.TEXT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["user", "-date"]),
        ]

    def __str__(self):
        return self.title
