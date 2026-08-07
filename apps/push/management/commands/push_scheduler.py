"""Daemon that runs the three daily push slots at the configured local times.

Usage:
    python manage.py push_scheduler [--jitter 60] [--batch-size 200]

Runs forever (intended as its own process/service, e.g. a separate Docker
container). At each scheduled local time — 09:00 (morning), 13:00 (afternoon),
22:00 (evening) by default, overridable via the FCM_*_AT settings — it invokes
``send_daily_notifications`` with that slot. PushLog dedupes, so a late
catch-up run (after a restart or downtime) can never send the same slot twice
in one day.
"""

import random
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


def _slot_hours():
    """Ordered list of (hour, minute, slot) tuples for the day."""
    def parse(value, fallback):
        try:
            hour, minute = (value or "").split(":")
            return int(hour), int(minute)
        except (ValueError, TypeError):
            return fallback

    morning = parse(getattr(settings, "FCM_MORNING_AT", None), (9, 0))
    afternoon = parse(getattr(settings, "FCM_AFTERNOON_AT", None), (13, 0))
    evening = parse(getattr(settings, "FCM_EVENING_AT", None), (22, 0))
    return [
        (morning[0], morning[1], "morning"),
        (afternoon[0], afternoon[1], "afternoon"),
        (evening[0], evening[1], "evening"),
    ]


def _next_occurrence(now):
    """The next (datetime, slot) after ``now`` in the current day cycle."""
    for hour, minute, slot in _slot_hours():
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate, slot
    # All slots passed today: roll to the first slot tomorrow.
    first = _slot_hours()[0]
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=first[0], minute=first[1], second=0, microsecond=0), first[2]


def _sleep_seconds_until(target, jitter_seconds):
    seconds = max(1, (target - timezone.localtime()).total_seconds())
    seconds += random.uniform(0, jitter_seconds)
    return max(1, int(seconds))


class Command(BaseCommand):
    help = "Forever-run scheduler for the three daily FCM push slots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--jitter",
            type=int,
            default=60,
            help="Random delay (seconds) added before each run to spread load.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=getattr(settings, "FCM_BATCH_SIZE", 200),
            help="Passed through to send_daily_notifications.",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            f"push_scheduler starting. Slots: "
            + ", ".join(
                f"{h:02d}:{m:02d} {s}" for h, m, s in _slot_hours()
            )
        )
        while True:
            now = timezone.localtime()
            target, slot = _next_occurrence(now)
            wait = _sleep_seconds_until(target, options["jitter"])
            self.stdout.write(
                f"next run: {slot} at {target:%Y-%m-%d %H:%M} "
                f"({wait}s from {now:%H:%M})"
            )
            time.sleep(wait)

            try:
                call_command(
                    "send_daily_notifications",
                    slot=slot,
                    batch_size=options["batch_size"],
                )
            except Exception:  # noqa: BLE001 - the scheduler must survive failures
                import logging

                logging.getLogger(__name__).exception("Daily push run failed for slot %s", slot)
