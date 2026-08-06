from datetime import date, timedelta

from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    google_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    bio = models.TextField(blank=True)
    phone_number = models.CharField(max_length=40, blank=True)
    birthday = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=32, blank=True)
    addresses = models.JSONField(default=list, blank=True)
    day_streak = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.display_name or self.user.get_username()

    def recompute_day_streak(self):
        """Consecutive days (ending today) with at least one journal entry.

        Any day without an entry breaks the streak: a missed day resets it to
        zero, otherwise every passing day with an entry adds one. Recomputes
        from the user's entries and persists the fresh value.
        """
        from apps.journals.models import JournalEntry

        dates = set(
            JournalEntry.objects.filter(user=self.user).values_list(
                "date", flat=True
            )
        )
        cursor = date.today()
        streak = 0
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        if streak != self.day_streak:
            self.day_streak = streak
            self.save(update_fields=["day_streak", "updated_at"])
        return streak
