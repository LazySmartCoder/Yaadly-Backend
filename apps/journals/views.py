import re
import threading
import uuid
from datetime import date as date_type
from urllib.parse import urlparse

import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import UserProfile

from .ollama import (
    ask_followup,
    build_bio,
    chat_reply,
    journalize,
    journalize_chat,
    journalize_chat_continuation,
    route_chat,
    summarize,
)
from .models import ChatSession, Gallery, JournalEntry
from .serializers import GallerySerializer, JournalEntrySerializer

logger = logging.getLogger(__name__)

# Serializes finalization for a given (user, date) inside this process. Two
# near-simultaneous calls (e.g. the app going inactive then paused) would
# otherwise both see the session as PENDING and run the local model twice,
# queueing two multi-minute Ollama jobs ahead of the user's next chat message.
_finalize_locks = {}
_finalize_locks_guard = threading.Lock()


def _finalize_lock(user_id, day):
    key = (user_id, day)
    with _finalize_locks_guard:
        lock = _finalize_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _finalize_locks[key] = lock
        return lock


def _one_line(raw_content):
    """A single collapsed line of the raw content."""
    line = " ".join((raw_content or "").split())
    return line[:1000]


def _day_entry(user, day):
    """The single journal entry that already exists for ``user``'s ``day``.

    A user gets exactly one entry per calendar day; every later conversation or
    thought on that day is merged into this entry instead of creating a new one.
    """
    return (
        JournalEntry.objects.filter(user=user, date=day)
        .order_by("created_at")
        .first()
    )


# Cues that the latest user message is reaching back into the journal. We only
# spend a routing model call when one of these matches; everyday chit-chat goes
# straight to the reply model. This keeps ordinary conversation from being
# misrouted into a "Are you talking about <date>?" confirmation.
_MEMORY_CUES = re.compile(
    r"\b(?:"
    r"remember|recall|memor(?:y|ies)|remind me|"
    r"that (?:day|time|night|trip|moment)|back then|"
    r"yesterday|last night|last week|last month|last year|"
    r"\d+\s+(?:day|week|month|year)s?\s+ago|"
    r"when i|when we|what did i|what was i|how was my|"
    r"my (?:journal|diary|entry|entries)|"
    r"look(?:ing)? back|think(?:ing)? back"
    r")\b",
    re.IGNORECASE,
)

# Any explicit date-ish reference ("November 8", "2026-11-08", "8th", "11/08").
_DATE_TEXT = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"(?:,?\s+\d{4})?\b"
    r"|\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\bthe\s+\d{1,2}(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)

_CONFIRMATION = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|correct|right|exactly|that'?s (?:it|right)"
    r"|no|nope|not really)\b",
    re.IGNORECASE,
)

_DATE_QUESTION = re.compile(
    r"(?:are you (?:talking|thinking) about|do you mean|which day|what date)",
    re.IGNORECASE,
)


def _latest_user_text(messages):
    for msg in reversed(messages):
        if (msg.get("role") or "").lower() == "user":
            return (msg.get("content") or "").strip()
    return ""


def _last_assistant_text(messages):
    for msg in reversed(messages):
        if (msg.get("role") or "").lower() == "assistant":
            return (msg.get("content") or "").strip()
    return ""


def _looks_like_memory_query(messages):
    """True only when the latest user turn plausibly refers to the past."""
    text = _latest_user_text(messages)
    if not text:
        return False
    if _MEMORY_CUES.search(text) or _DATE_TEXT.search(text):
        return True
    # A short yes/no answering our own date-confirmation is still a memory
    # turn and must be routed so the chosen date can be resolved.
    if _CONFIRMATION.match(text) and _DATE_QUESTION.search(
        _last_assistant_text(messages)
    ):
        return True
    return False


def refresh_bio(user):
    """Rebuild the user's two-line bio from their latest entries and store
    it on their profile. Best-effort: any local-model/network failure leaves
    the existing bio untouched."""
    contents = list(
        JournalEntry.objects.filter(user=user)
        .order_by("-date", "-created_at")
        .values_list("content", flat=True)[:5]
    )
    bio = build_bio("\n\n".join(reversed(contents)))
    if not bio:
        return
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.bio = bio
    profile.save(update_fields=["bio", "updated_at"])


def refresh_day_streak(user):
    """Recompute the user's day streak from their entries and persist it on
    their profile (a missed day resets it to zero)."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.recompute_day_streak()


class JournalEntryViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = JournalEntrySerializer
    search_fields = ["title", "content", "mood"]
    ordering_fields = ["date", "created_at", "title"]

    def get_queryset(self):
        return JournalEntry.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        raw = (serializer.validated_data.get("content") or "").strip()
        day = serializer.validated_data.get("date") or timezone.localdate()

        # One entry per day: if this day already has an entry, fold the new
        # thoughts into it instead of creating a second entry for the same day.
        existing = _day_entry(self.request.user, day)
        if existing is not None:
            combined = "\n\n".join(
                part for part in (existing.content.strip(), raw) if part
            )
            enriched = journalize(combined)
            if enriched is None:
                # Local model unavailable: keep the merged words and a title.
                title = (serializer.validated_data.get("title") or "").strip()
                if title:
                    existing.title = title
                existing.content = combined
                existing.save()
            else:
                existing.title = enriched["title"] or existing.title
                existing.content = enriched["content"]
                existing.mood = enriched["mood"] or existing.mood
                existing.save()
            serializer.instance = existing
        else:
            enriched = journalize(raw)
            if enriched is None:
                # Local model unavailable: keep the user's words and derive a title.
                title = (serializer.validated_data.get("title") or "").strip()
                if not title:
                    first_line = raw.splitlines()[0].strip() if raw else ""
                    title = (first_line[:255] if first_line else "Untitled entry")
                serializer.save(
                    user=self.request.user,
                    title=title,
                )
            else:
                serializer.save(
                    user=self.request.user,
                    title=enriched["title"] or "Untitled entry",
                    content=enriched["content"],
                    mood=enriched["mood"],
                )
        self._refresh_bio(self.request.user)
        self._refresh_day_streak(self.request.user)

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)
        refresh_day_streak(self.request.user)

    def perform_destroy(self, instance):
        self._delete_gallery_files(instance)
        super().perform_destroy(instance)
        refresh_day_streak(self.request.user)

    @staticmethod
    def _delete_gallery_files(entry):
        """Best-effort: remove each attached photo from object storage before
        the entry (and its Gallery rows) are deleted."""
        for photo in entry.gallery_photos.all():
            key = _storage_key(photo.url)
            if not key:
                continue
            try:
                default_storage.delete(key)
            except Exception:
                pass

    @staticmethod
    def _refresh_bio(user):
        return refresh_bio(user)

    @staticmethod
    def _refresh_day_streak(user):
        return refresh_day_streak(user)


class EntryPhotosView(APIView):
    """List and upload photos attached to a single journal entry.

    Uploaded files are persisted to the configured object storage bucket
    under `gallery/<entry id>/<uuid>.<ext>` and the public URL returned by the
    storage backend is stored on a Gallery row linked to the entry.
    """

    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self, entry_id):
        entry = get_object_or_404(
            JournalEntry, user=self.request.user, id=entry_id
        )
        return entry.gallery_photos.all()

    def get(self, request, entry_id):
        photos = self.get_queryset(entry_id)
        return Response(GallerySerializer(photos, many=True).data)

    def post(self, request, entry_id):
        entry = get_object_or_404(
            JournalEntry, user=self.request.user, id=entry_id
        )
        files = request.FILES.getlist("photos")
        if not files:
            return Response(
                {"detail": "At least one photo is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = []
        try:
            for file in files:
                name = f"gallery/{entry.id}/{uuid.uuid4().hex}{_image_extension(file)}"
                stored = default_storage.save(
                    name,
                    ContentFile(file.read(), name=name),
                )
                url = default_storage.url(stored)
                created.append(
                    Gallery.objects.create(entry=entry, user=entry.user, url=url)
                )
        except Exception:
            logger.exception("Photo upload failed for entry %s", entry.id)
            # Roll back partial uploads so a failed request leaves nothing behind.
            for photo in created:
                try:
                    key = _storage_key(photo.url)
                    if key:
                        default_storage.delete(key)
                except Exception:
                    pass
                photo.delete()
            return Response(
                {"detail": "Could not upload the photos. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            GallerySerializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )


class ChatSessionPhotosView(APIView):
    """Persist photos for a day's chat, attaching them to its journal when available."""

    parser_classes = [MultiPartParser, FormParser]

    def _session(self, request, raw_date):
        try:
            day = date_type.fromisoformat(raw_date)
        except ValueError:
            return None, Response(
                {"detail": "Date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        session, _ = ChatSession.objects.get_or_create(user=request.user, date=day)
        return session, None

    def get(self, request):
        session, error = self._session(
            request, (request.query_params.get("date") or "").strip()
        )
        if error:
            return error
        photos = Gallery.objects.filter(chat_session=session)
        if session.journal_entry_id is not None:
            photos = Gallery.objects.filter(
                Q(chat_session=session) | Q(entry_id=session.journal_entry_id)
            )
        return Response(GallerySerializer(photos, many=True).data)

    def post(self, request):
        session, error = self._session(
            request, (request.data.get("date") or "").strip()
        )
        if error:
            return error
        files = request.FILES.getlist("photos")
        if not files:
            return Response(
                {"detail": "At least one photo is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entry = session.journal_entry
        created = []
        try:
            for file in files:
                parent = str(entry.id) if entry else f"chat/{session.id}"
                name = (
                    f"gallery/{parent}/{uuid.uuid4().hex}"
                    f"{_image_extension(file)}"
                )
                stored = default_storage.save(
                    name,
                    ContentFile(file.read(), name=name),
                )
                created.append(
                    Gallery.objects.create(
                        entry=entry,
                        chat_session=None if entry else session,
                        user=request.user,
                        url=default_storage.url(stored),
                    )
                )
        except Exception:
            logger.exception(
                "Photo upload failed for chat session %s", session.id
            )
            for photo in created:
                try:
                    key = _storage_key(photo.url)
                    if key:
                        default_storage.delete(key)
                except Exception:
                    logger.exception("Could not remove partial chat photo upload")
                photo.delete()
            return Response(
                {"detail": "Could not upload the photos. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            GallerySerializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )


def _storage_key(url):
    """Derives the storage name a Gallery URL points at, handling S3-style
    public URLs (which include the bucket name) and the local MEDIA_URL
    prefix."""
    if not url:
        return ""
    key = urlparse(url).path.lstrip("/")
    media = settings.MEDIA_URL.strip("/")
    if media and key.startswith(media + "/"):
        key = key[len(media) + 1:]
    bucket = getattr(default_storage, "bucket_name", None)
    if isinstance(bucket, str) and key.startswith(bucket + "/"):
        key = key[len(bucket) + 1:]
    return key


def _image_extension(file):
    """Best-effort file extension for the bucket key, defaulting to .jpg."""
    name = (getattr(file, "name", "") or "").lower()
    if "." in name:
        ext = name.rsplit(".", 1)[-1]
        if ext in {"jpg", "jpeg", "png", "gif", "webp", "heic", "bmp"}:
            return f".{ext}"
    return ".jpg"


class FollowUpView(APIView):
    def post(self, request):
        content = (request.data.get("content") or "").strip()
        question = ask_followup(content) if content else None
        return Response({"question": question or ""})


class StatsView(APIView):
    """Aggregate journal counts computed on the fly from the database.

    Nothing is persisted here: every value is derived from the user's existing
    rows, so it can never drift out of sync with the entries themselves.
    """

    def get(self, request):
        entries = JournalEntry.objects.filter(user=request.user)
        now = timezone.now()
        return Response(
            {
                "total_entries": entries.count(),
                "entries_this_month": entries.filter(
                    created_at__year=now.year,
                    created_at__month=now.month,
                ).count(),
                "total_photos": Gallery.objects.filter(
                    Q(user=request.user) | Q(entry__user=request.user)
                ).count(),
            }
        )


class TodayDropsView(APIView):
    """Global count of journal entries created today, across all users.

    Powers the real-time "Today's Drops" counter on the home screen. Public on
    purpose: it exposes no personal data, only a single aggregate number, so the
    app can show it without waiting on an authenticated round-trip.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        today = timezone.localdate()
        count = JournalEntry.objects.filter(
            created_at__year=today.year,
            created_at__month=today.month,
            created_at__day=today.day,
        ).count()
        return Response({"count": count})


class ChatView(APIView):
    """Continue the selected day's backend-stored conversation with the user.

    Everyday chat stays in the present: the AI simply talks to the user and
    mirrors their mood (happy, sad, playful, emotional, ...) without surfacing
    journal entries. A cheap heuristic first checks whether the latest message
    actually reaches back into the journal; only then does the backend route
    through the journal index. General chit-chat answers straight from the
    conversation, references to a specific day first confirm the date, and
    confirmed days pull the full title + content before answering. Routing and
    replies both use the local Ollama model. Best-effort: any failure falls
    back to a gentle reply.
    """

    def post(self, request):
        session = None
        raw_date = request.data.get("date")
        latest_message = request.data.get("message")
        if raw_date is not None or latest_message is not None:
            if not isinstance(raw_date, str) or not isinstance(latest_message, str):
                return Response(
                    {"detail": "A date and message are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                day = date_type.fromisoformat(raw_date.strip())
            except ValueError:
                return Response(
                    {"detail": "Date must be in YYYY-MM-DD format."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            latest_message = latest_message.strip()
            if not latest_message:
                return Response(
                    {"detail": "A non-empty message is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            session, _ = ChatSession.objects.get_or_create(
                user=request.user,
                date=day,
            )
            messages = [
                message
                for message in (session.messages or [])
                if isinstance(message, dict)
                and message.get("role") in {"user", "assistant"}
                and isinstance(message.get("content"), str)
                and message["content"].strip()
            ]
            messages.append({"role": "user", "content": latest_message})
            session.messages = messages
            session.save(update_fields=["messages", "updated_at"])
        else:
            messages = request.data.get("messages")

        if not isinstance(messages, list) or not messages:
            return Response(
                {"detail": "A non-empty messages list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def response_with_saved_turn(reply):
            if session is not None:
                session.messages = [
                    *messages,
                    {"role": "assistant", "content": reply},
                ]
                session.save(update_fields=["messages", "updated_at"])
            return Response({"reply": reply})

        latest = _latest_user_text(messages)
        logger.info(
            "Chat request from user %s: %d message(s); latest prompt=%r",
            request.user.id,
            len(messages),
            latest[:200],
        )

        entries = list(
            JournalEntry.objects.filter(user=request.user).order_by(
                "-date", "-created_at"
            )[:50]
        )
        if not entries:
            # No journal yet — still chat warmly in the present. The model
            # knows there is no memory context and will say so if asked.
            reply = chat_reply(messages, memories=None)
            return response_with_saved_turn(
                reply or "I'm always here to listen. What's on your mind?"
            )

        # Everyday chat and new thoughts answer straight from the conversation.
        # Only messages that plausibly reach back into the journal are routed,
        # so a normal message can never become a date-confirmation question.
        if not _looks_like_memory_query(messages):
            logger.info("Chat routing skipped (everyday message)")
            intended = "general"
            reply = chat_reply(messages, memories=None)
        else:
            entry_by_date = {entry.date.isoformat(): entry for entry in entries}
            index = "\n".join(
                f"{entry.date.isoformat()}: "
                f"{entry.title or _one_line(entry.content)[:120]}"
                for entry in entries
            )

            route = route_chat(messages, index) or {}
            intended = route.get("intent", "general")
            entry = entry_by_date.get(route.get("date") or "")

            if intended == "detail" and entry is not None:
                memories = (
                    f"Date: {entry.date.isoformat()}\n"
                    f"Title: {entry.title}\n"
                    f"Mood: {entry.mood or '(none)'}\n"
                    f"Content:\n{entry.content}"
                )
                reply = chat_reply(messages, memories)
            elif intended == "ask_date" and entry is not None:
                # Ask with the entry's own date and title so the question is
                # grounded in something the user can actually recognise.
                label = entry.title or _one_line(entry.content)[:60]
                reply = f"Wait, do you mean {entry.date:%B %d, %Y}"
                reply += f' — the day with "{label}"?' if label else "?"
            else:
                if intended != "general":
                    logger.info(
                        "Chat routing: %s for unresolved date %r; "
                        "answering generally instead.",
                        intended,
                        route.get("date"),
                    )
                intended = "general"
                reply = chat_reply(messages, memories=None)

        if not reply:
            logger.warning(
                "Chat reply unavailable (intent=%s); sending fallback.", intended
            )
            return response_with_saved_turn(
                "I'm always here to listen. What's on your mind?"
            )
        logger.info("Chat reply ready (intent=%s)", intended)
        return response_with_saved_turn(reply)


class ChatSessionView(APIView):
    """Read or replace the authenticated user's transcript for one calendar day."""

    def get(self, request):
        raw_date = (request.query_params.get("date") or "").strip()
        try:
            day = date_type.fromisoformat(raw_date)
        except ValueError:
            return Response(
                {"detail": "Date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = ChatSession.objects.filter(user=request.user, date=day).first()
        return Response(
            {
                "date": day.isoformat(),
                "messages": session.messages if session is not None else [],
                "journalized_message_count": (
                    session.journalized_message_count if session is not None else 0
                ),
            }
        )

    def post(self, request):
        raw_date = (request.data.get("date") or "").strip()
        try:
            day = date_type.fromisoformat(raw_date)
        except ValueError:
            return Response(
                {"detail": "Date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        messages = request.data.get("messages")
        if not isinstance(messages, list):
            return Response(
                {"detail": "A messages list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        transcript = [
            {
                "role": message.get("role"),
                "content": (message.get("content") or "").strip(),
            }
            for message in messages
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and (message.get("content") or "").strip()
        ]
        if not transcript or not any(
            message["role"] == "user" for message in transcript
        ):
            return Response(
                {"detail": "A transcript with user messages is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session, _ = ChatSession.objects.get_or_create(
            user=request.user,
            date=day,
        )
        session.messages = transcript
        session.save(update_fields=["messages", "updated_at"])
        return Response({"date": day.isoformat(), "messages": session.messages})


class ChatEodView(APIView):
    """Merge new chat messages into that day's journal, immediately or at five turns."""

    def post(self, request):
        raw_date = (request.data.get("date") or "").strip()
        messages = request.data.get("messages")
        force = request.data.get("force", True)
        if not isinstance(force, bool):
            return Response(
                {"detail": "Force must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not raw_date:
            return Response(
                {"detail": "A date is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if messages is not None and (not isinstance(messages, list) or not messages):
            return Response(
                {"detail": "Messages must be a non-empty list when provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            day = date_type.fromisoformat(raw_date)
        except ValueError:
            return Response(
                {"detail": "Date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if day > timezone.localdate():
            return Response(
                {"detail": "Cannot finalize a future day window."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        incoming_transcript = None
        if messages is not None:
            incoming_transcript = [
                {
                    "role": msg.get("role"),
                    "content": (msg.get("content") or "").strip(),
                }
                for msg in messages
                if isinstance(msg, dict)
                and msg.get("role") in {"user", "assistant"}
                and (msg.get("content") or "").strip()
            ]
            if not any(message["role"] == "user" for message in incoming_transcript):
                return Response(
                    {"detail": "A transcript with user messages is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with _finalize_lock(request.user.id, day):
            session = ChatSession.objects.filter(
                user=request.user,
                date=day,
            ).first()
            if session is None:
                if incoming_transcript is None:
                    return Response(
                        {
                            "entry": None,
                            "finalized": False,
                            "journalized_message_count": 0,
                        }
                    )
                session = ChatSession.objects.create(
                    user=request.user,
                    date=day,
                    messages=incoming_transcript,
                )
            elif incoming_transcript is not None:
                session.messages = incoming_transcript
                session.save(update_fields=["messages", "updated_at"])

            transcript = session.messages or []
            if not any(message["role"] == "user" for message in transcript):
                return Response(
                    {"detail": "A transcript with user messages is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            logger.info(
                "EOD finalize request from user %s for %s: %d message(s)",
                request.user.id,
                day.isoformat(),
                len(transcript),
            )

            processed_count = min(
                session.journalized_message_count,
                len(transcript),
            )
            if processed_count == len(transcript):
                entry = session.journal_entry or _day_entry(request.user, day)
                return Response(
                    {
                        "entry": (
                            JournalEntrySerializer(entry).data
                            if entry is not None
                            else None
                        ),
                        "finalized": entry is not None,
                        "journalized_message_count": processed_count,
                    }
                )

            new_messages = transcript[processed_count:]
            pending_user_chats = sum(
                message.get("role") == "user" for message in new_messages
            )
            if not force and pending_user_chats < 5:
                entry = session.journal_entry or _day_entry(request.user, day)
                return Response(
                    {
                        "entry": (
                            JournalEntrySerializer(entry).data
                            if entry is not None
                            else None
                        ),
                        "finalized": False,
                        "pending_user_chats": pending_user_chats,
                        "journalized_message_count": processed_count,
                    }
                )

            entry = session.journal_entry or _day_entry(request.user, day)
            if entry is not None:
                enriched = journalize_chat_continuation(entry, new_messages)
                if enriched is None:
                    return Response(
                        {
                            "entry": JournalEntrySerializer(entry).data,
                            "finalized": False,
                            "error": "Could not journalize the conversation yet.",
                        },
                        status=status.HTTP_200_OK,
                    )

                entry.title = enriched.get("title") or entry.title
                entry.content = enriched.get("content") or entry.content
                entry.mood = enriched.get("mood") or entry.mood
                entry.save()
            else:
                enriched = journalize_chat(new_messages, date=day)
                if enriched is None:
                    return Response(
                        {
                            "entry": None,
                            "finalized": False,
                            "error": "Could not journalize the conversation yet.",
                        },
                        status=status.HTTP_200_OK,
                    )

                entry = JournalEntry.objects.create(
                    user=request.user,
                    date=day,
                    title=enriched.get("title") or "Day of conversation",
                    content=enriched.get("content") or "",
                    mood=enriched.get("mood") or "",
                    type=JournalEntry.EntryType.TEXT,
                )

            session.journal_entry = entry
            Gallery.objects.filter(chat_session=session).update(
                entry=entry,
                chat_session=None,
            )
            session.journalized_message_count = len(transcript)
            session.status = ChatSession.Status.FINALIZED
            session.save(
                update_fields=[
                    "journal_entry",
                    "journalized_message_count",
                    "status",
                    "updated_at",
                ]
            )
            refresh_bio(request.user)
            refresh_day_streak(request.user)
            return Response(
                {
                    "entry": JournalEntrySerializer(entry).data,
                    "finalized": True,
                    "journalized_message_count": len(transcript),
                }
            )


class SummaryView(APIView):
    """Home nudge based on the user's most recent journal entry: a follow-up
    question (with a reflective sentence) tone-matched to the entry's mood."""

    def get(self, request):
        latest = (
            JournalEntry.objects.filter(user=request.user)
            .order_by("-date", "-created_at")
            .first()
        )
        if latest is None:
            return Response({}, status=status.HTTP_204_NO_CONTENT)
        result = summarize(latest.content, mood=latest.mood)
        if result is None:
            return Response({}, status=status.HTTP_204_NO_CONTENT)
        return Response(result)
