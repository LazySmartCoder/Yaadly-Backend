"""Admin-triggered push campaigns.

Today that is the "Shoot" action: send every user a "Remember?" notification
with a photo from a random one of their journal entries. The logic lives here
(not in the view) so it can be reused by a management command or a scheduler
later and is easy to unit-test.
"""

import logging

from django.db.models import Exists, OuterRef

from apps.journals.models import Gallery, JournalEntry
from apps.journals.serializers import _fresh_url

from .models import DeviceToken
from .services import DeviceNotRegistered, FcmError, send_message

logger = logging.getLogger(__name__)

TITLE = "Remember?"
MAX_PHOTO_ENTRY_ATTEMPTS = 5


def _random_entry_with_photo(user):
    """A random journal entry of ``user`` that has at least one photo, or
    None. Only entries with at least one Gallery row carrying a real URL are
    eligible, so the notification never references an entry without an image.
    A correlated Exists subquery (instead of ``gallery_photos__isnull=False``)
    keeps the match exact even if the entry has other photo rows, and avoids
    the ``distinct()`` + ``order_by("?")`` combination that PostgreSQL does not
    combine reliably."""
    has_photo = Gallery.objects.filter(
        entry=OuterRef("pk"),
    ).exclude(url="")
    return (
        JournalEntry.objects.filter(user=user)
        .filter(Exists(has_photo))
        .order_by("?")
        .first()
    )


def _remember_body(entry):
    """The push body for an entry, e.g. 'Remember this photo from 5 August?'"""
    return f"Remember this photo from {entry.date.day} {entry.date:%B}?"


def send_remember_pushes():
    """Send every user with an active device token a photo "Remember?" push.

    Each user gets one push targeted at their own active tokens, built from a
    random journal entry of theirs that has photos (latest photo of that
    entry). Users without any such entry are skipped. Best-effort per token: an
    unregistered token is deactivated so it is not retried, other failures are
    logged and skipped.

    Returns a summary dict with the counters the admin view reports back.
    """
    active_tokens = list(
        DeviceToken.objects.filter(is_active=True)
        .select_related("user")
        .order_by("user_id")
    )
    tokens_by_user = {}
    for token in active_tokens:
        tokens_by_user.setdefault(token.user_id, []).append(token)

    summary = {
        "users_targeted": 0,
        "users_skipped": 0,
        "tokens_sent": 0,
        "tokens_failed": 0,
    }

    for user_id, tokens in tokens_by_user.items():
        user = tokens[0].user
        entry = _random_entry_with_photo(user)
        if entry is None:
            summary["users_skipped"] += 1
            logger.info(
                "Shoot: no photo entry for user %s; skipping", user_id
            )
            continue

        photo = entry.gallery_photos.order_by("-created_at").first()
        if photo is None or not photo.url.strip():
            summary["users_skipped"] += 1
            logger.info(
                "Shoot: no usable photo for user %s (entry %s); skipping",
                user_id,
                entry.id,
            )
            continue

        summary["users_targeted"] += 1
        image_url = _fresh_url(photo.url)
        body = _remember_body(entry)
        logger.info(
            "Shoot: %s token(s) for user %s from entry %s",
            len(tokens),
            user_id,
            entry.id,
        )
        for token in tokens:
            try:
                send_message(
                    token.token,
                    TITLE,
                    body,
                    data={"type": "remember", "entry_id": str(entry.id)},
                    image=image_url,
                )
                summary["tokens_sent"] += 1
            except DeviceNotRegistered:
                summary["tokens_failed"] += 1
                token.is_active = False
                token.save(update_fields=["is_active", "updated_at"])
            except FcmError as exc:
                summary["tokens_failed"] += 1
                logger.warning(
                    "Shoot: FCM send failed for user %s token %s...: %s",
                    user_id,
                    token.token[:24],
                    exc,
                )

    logger.info(
        "Shoot finished: %s", summary
    )
    return summary
