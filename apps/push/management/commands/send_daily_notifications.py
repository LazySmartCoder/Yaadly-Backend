"""Send one of the three daily FCM push notifications to every user in batches.

Usage:
    python manage.py send_daily_notifications                     # auto slot
    python manage.py send_daily_notifications --slot morning      # 9 AM
    python manage.py send_daily_notifications --slot afternoon    # 1 PM
    python manage.py send_daily_notifications --slot evening      # 10 PM
    python manage.py send_daily_notifications --slot morning --batch-size 100 --dry-run

Users are processed in batches (the batch size defaults to settings.FCM_BATCH_SIZE)
to keep each run bounded and the output readable. Every slot runs at most once
per user per day: a PushLog row is recorded once at least one device got the
message, so re-running the command (or a catch-up run after downtime) never
duplicates a notification.
"""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.journals.models import JournalEntry
from apps.push.gemini import (
    afternoon_memory,
    evening_message,
    morning_message,
)
from apps.push.models import DeviceToken, PushLog
from apps.push.services import (
    DeviceNotRegistered,
    FcmError,
    FcmNotConfigured,
    is_configured,
    send_message,
)

logger = logging.getLogger(__name__)

# Slot name -> local hour(s) when it runs (used by --auto slot selection).
SLOT_HOURS = {
    "morning": 9,
    "afternoon": 13,
    "evening": 22,
}

# How many journal entries feed the AI copy for one user, and the per-entry
# character cap, so a single Gemini call stays small and cheap.
CONTEXT_ENTRY_LIMIT = 10
CONTEXT_ENTRY_CHARS = 1000
MEMORY_WINDOW_DAYS = 10


def current_slot():
    """The daily slot whose hour it is right now (server-local), or None."""
    hour = timezone.localtime().hour
    for slot, slot_hour in SLOT_HOURS.items():
        if hour == slot_hour:
            return slot
    return None


def _past_entries(user, today):
    """Recent journal content written BEFORE today, as compact text."""
    entries = (
        JournalEntry.objects.filter(user=user, date__lt=today)
        .order_by("-date", "-created_at")[:CONTEXT_ENTRY_LIMIT]
    )
    parts = []
    for entry in entries:
        content = " ".join((entry.content or "").split())
        if len(content) > CONTEXT_ENTRY_CHARS:
            content = content[:CONTEXT_ENTRY_CHARS] + "…"
        parts.append(f"{entry.date.isoformat()}: {content}")
    return "\n\n".join(parts)


def _random_recent_entry(user, today):
    """A random entry from the last few days (memory window), or None."""
    start = today - timedelta(days=MEMORY_WINDOW_DAYS)
    ids = list(
        JournalEntry.objects.filter(
            user=user,
            date__gte=start,
            date__lte=today,
        ).values_list("id", flat=True)
    )
    if not ids:
        return None
    return (
        JournalEntry.objects.filter(pk=random.choice(ids))
        .only("date", "mood", "content", "title")
        .first()
    )


def generate_for(slot, user, today):
    """Build the {title, body} notification copy for one user and slot."""
    if slot == "morning":
        return morning_message(_past_entries(user, today))
    if slot == "afternoon":
        return afternoon_memory(_random_recent_entry(user, today))
    if slot == "evening":
        return evening_message(_past_entries(user, today))
    raise CommandError(f"Unknown notification slot: {slot}")


class Command(BaseCommand):
    help = "Send the daily automated FCM push notifications in batches."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slot",
            choices=[c[0] for c in PushLog.Slot.choices],
            default=None,
            help="Which daily notification to send. Defaults to the slot for the current server-local hour.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=getattr(settings, "FCM_BATCH_SIZE", 200),
            help="Users to process per batch (default: settings.FCM_BATCH_SIZE).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate copy and log what would be sent, without calling FCM.",
        )

    def handle(self, *args, **options):
        slot = options["slot"] or current_slot()
        if not slot:
            self.stdout.write("No daily notification is due at this hour; nothing to do.")
            return
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1.")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"DRY-RUN: {slot} slot"))
        else:
            self.stdout.write(f"Sending {slot} notifications")

        if not options["dry_run"] and not is_configured():
            raise CommandError(
                "FCM is not configured (set FCM_SERVICE_ACCOUNT_PATH/FCM_SERVICE_ACCOUNT_JSON "
                "and FCM_PROJECT_ID). Nothing was sent."
            )

        today = timezone.localdate()
        try:
            self._send_slot(slot, today, options)
        except FcmNotConfigured as exc:
            # Credentials disappeared mid-run; stop with a clear message.
            raise CommandError(f"FCM became unavailable mid-run: {exc}")

    def _send_slot(self, slot, today, options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        user_ids = (
            DeviceToken.objects.filter(is_active=True)
            .values_list("user_id", flat=True)
            .distinct()
            .order_by("user_id")
        )
        already_sent = set(
            PushLog.objects.filter(slot=slot, date=today).values_list("user_id", flat=True)
        )
        pending = [uid for uid in user_ids if uid not in already_sent]

        total = len(pending)
        self.stdout.write(f"{total} user(s) pending for {slot} (batch size {batch_size})")
        if total == 0:
            return

        for batch_index, offset in enumerate(range(0, total, batch_size), start=1):
            batch = pending[offset : offset + batch_size]
            sent_in_batch = 0
            for position, user_id in enumerate(batch, start=1):
                self.stdout.write(
                    f"[{slot}] batch {batch_index}: user {position}/{len(batch)} "
                    f"(global {offset + position}/{total})"
                )
                user = self._user(user_id)
                if user is None:
                    continue
                if self._handle_user(user, slot, today, dry_run):
                    sent_in_batch += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{slot}] batch {batch_index} done: {sent_in_batch} user(s) notified"
                )
            )

    @staticmethod
    def _user(user_id):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(pk=user_id).first()

    def _handle_user(self, user, slot, today, dry_run):
        tokens = list(DeviceToken.objects.filter(user=user, is_active=True))
        if not tokens:
            return False

        try:
            copy = generate_for(slot, user, today)
        except Exception:  # noqa: BLE001 - AI copy must never sink a run
            logger.exception("Push copy generation failed for user %s", user.id)
            copy = None
        if not copy:
            return False

        if dry_run:
            self.stdout.write(
                f"  would send -> {copy['title']} | {copy['body']!r} "
                f"({len(tokens)} device(s))"
            )
            return True

        data = {"slot": slot, "date": today.isoformat()}
        delivered = 0
        for token in tokens:
            try:
                send_message(token.token, copy["title"], copy["body"], data=data)
                delivered += 1
            except DeviceNotRegistered:
                token.is_active = False
                token.save(update_fields=["is_active", "updated_at"])
                logger.info("Deactivated unregistered device token for user %s", user.id)
            except FcmNotConfigured:
                raise
            except FcmError as exc:
                logger.warning("Push failed for user %s device %s: %s", user.id, token.id, exc)

        if delivered == 0:
            return False

        PushLog.objects.create(user=user, slot=slot, date=today)
        return True
