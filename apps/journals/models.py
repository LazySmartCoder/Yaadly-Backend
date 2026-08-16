import uuid

from django.conf import settings
from django.db import models


class JournalEntry(models.Model):
    class EntryType(models.TextChoices):
        TEXT = "text", "Text"

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
    aid = models.TextField(
        blank=True,
        default="",
        help_text="Long, detailed one-line description of the entry.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["user", "-date"]),
        ]

    def __str__(self):
        return self.title


class ChatSession(models.Model):
    """A single day window (00:00-00:00, device-local) of AI chat transcript.

    The phone holds the day's chat in its local cache while the day is live.
    When the day window ends, the app finalizes the transcript here exactly
    once: the backend stores the raw messages, journalizes them via Gemini and
    saves the result as a normal JournalEntry for that date. The row (and the
    ``(user, date)`` uniqueness) make finalization idempotent, so a retry can
    never duplicate the day's journal.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FINALIZED = "finalized", "Finalized"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_sessions",
    )
    date = models.DateField()
    messages = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_session",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "date"],
                name="unique_user_chat_day",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.date.isoformat()} ({self.status})"


class Gallery(models.Model):
    """A photo attached to a journal entry.

    The image itself lives in the configured object storage bucket; this row
    only stores the public URL the object storage returns for it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="gallery_photos",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gallery_photos",
        null=True,
    )
    url = models.URLField(max_length=2048)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.url
