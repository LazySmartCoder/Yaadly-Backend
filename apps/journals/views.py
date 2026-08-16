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

from .gemini import (
    ask_followup,
    build_bio,
    chat_reply,
    describe_entry,
    journalize,
    journalize_chat,
    route_chat,
    summarize,
)
from .models import ChatSession, Gallery, JournalEntry
from .serializers import GallerySerializer, JournalEntrySerializer

logger = logging.getLogger(__name__)


def _one_line(raw_content):
    """Local fallback for AID: a single collapsed line of the raw content."""
    line = " ".join((raw_content or "").split())
    return line[:1000]


def refresh_bio(user):
    """Rebuild the user's two-line bio from their latest entries and store
    it on their profile. Best-effort: any Gemini/network failure leaves the
    existing bio untouched."""
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
        enriched = journalize(raw)
        if enriched is None:
            # Gemini unavailable: keep the user's words and derive a title.
            title = (serializer.validated_data.get("title") or "").strip()
            if not title:
                first_line = raw.splitlines()[0].strip() if raw else ""
                title = (first_line[:255] if first_line else "Untitled entry")
            serializer.save(
                user=self.request.user,
                title=title,
                aid=describe_entry(raw) or _one_line(raw),
            )
        else:
            serializer.save(
                user=self.request.user,
                title=enriched["title"] or "Untitled entry",
                content=enriched["content"],
                mood=enriched["mood"],
                aid=describe_entry(enriched["content"], enriched["mood"])
                or _one_line(raw),
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
    """Carry on a warm conversation with the user.

    Everyday chat stays in the present: the AI simply talks to the user and
    mirrors their mood (happy, sad, playful, emotional, ...) without surfacing
    journal entries. Only when the user asks to recall a memory does the
    backend route to the journal index: general chit-chat answers straight
    from the conversation, references to a specific day first confirm the
    date, and confirmed days pull the full title + content before answering.
    Routing and replies both use the cheap flash-lite model. Best-effort: any
    failure falls back to a gentle reply.
    """

    def post(self, request):
        messages = request.data.get("messages")
        if not isinstance(messages, list) or not messages:
            return Response(
                {"detail": "A non-empty messages list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entries = list(
            JournalEntry.objects.filter(user=request.user).order_by(
                "-date", "-created_at"
            )
        )
        if not entries:
            # No journal yet — still chat warmly in the present. The model
            # knows there is no memory context and will say so if asked.
            reply = chat_reply(messages, memories=None)
            return Response(
                {"reply": reply or "I'm always here to listen. What's on your mind?"}
            )

        index = "\n".join(
            f"{entry.date.isoformat()}: "
            f"{entry.aid or entry.title or _one_line(entry.content)[:120]}"
            for entry in entries
        )

        route = route_chat(messages, index)
        intent = (route or {}).get("intent", "general")

        if intent == "detail":
            date = (route or {}).get("date")
            entry = next(
                (e for e in entries if e.date.isoformat() == date),
                None,
            )
            if entry is not None:
                memories = (
                    f"Date: {entry.date.isoformat()}\n"
                    f"Title: {entry.title}\n"
                    f"Mood: {entry.mood or '(none)'}\n"
                    f"Content:\n{entry.content}"
                )
                reply = chat_reply(messages, memories)
            else:
                reply = chat_reply(messages, None)
        elif intent == "ask_date":
            question = (route or {}).get("question")
            reply = question or (
                "Hmm, which day are you thinking of? Tell me the date "
                "and I'll revisit it with you."
            )
        else:
            # Everyday chat: no journal entries are surfaced. The AI simply
            # keeps talking and matches the user's mood.
            reply = chat_reply(messages, memories=None)

        if not reply:
            return Response(
                {"reply": "I'm always here to listen. What's on your mind?"}
            )
        return Response({"reply": reply})


class ChatEodView(APIView):
    """Finalize a finished 24-hour chat window (00:00-00:00, device-local date)
    into a journal entry.

    The phone caches the day's AI chat transcript locally and calls this once
    when the day rolls over. The raw transcript is stored on a ChatSession row
    (unique per user + date, so retries never duplicate the journal) and
    journalized with the cheap flash-lite model into a first-person diary entry
    that weaves in the AI dialogue. The resulting entry is saved as a normal
    JournalEntry for that date, so it shows up everywhere a journal does. When
    Gemini is unavailable the session stays pending and the app retries later.
    """

    def post(self, request):
        raw_date = (request.data.get("date") or "").strip()
        messages = request.data.get("messages")
        if not raw_date:
            return Response(
                {"detail": "A date is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(messages, list) or not messages:
            return Response(
                {"detail": "A non-empty messages list is required."},
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

        transcript = [
            {
                "role": msg.get("role"),
                "content": (msg.get("content") or "").strip(),
            }
            for msg in messages
            if isinstance(msg, dict)
            and msg.get("role") in {"user", "assistant"}
            and (msg.get("content") or "").strip()
        ]
        if not any(m["role"] == "user" for m in transcript):
            return Response(
                {"detail": "A transcript with user messages is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session, _ = ChatSession.objects.get_or_create(
            user=request.user,
            date=day,
            defaults={"messages": transcript},
        )

        # Idempotent: an already-finalized day just returns its journal.
        if session.status == ChatSession.Status.FINALIZED and session.journal_entry_id:
            return Response(
                {
                    "entry": JournalEntrySerializer(session.journal_entry).data,
                    "finalized": True,
                }
            )

        enriched = journalize_chat(transcript, date=day)
        if enriched is None:
            # Keep the session pending so the app retries on its next launch.
            if session.status == ChatSession.Status.PENDING:
                session.messages = transcript
                session.save(update_fields=["messages", "updated_at"])
            return Response(
                {
                    "entry": None,
                    "finalized": False,
                    "error": "Could not journalize the conversation yet.",
                },
                status=status.HTTP_200_OK,
            )

        entry = session.journal_entry
        if entry is None:
            entry = JournalEntry.objects.create(
                user=request.user,
                date=day,
                title=enriched.get("title") or "Day of conversation",
                content=enriched.get("content") or "",
                mood=enriched.get("mood") or "",
                type=JournalEntry.EntryType.TEXT,
                aid=enriched.get("aid") or _one_line(
                    " ".join(m["content"] for m in transcript)
                ),
            )
        else:
            entry.title = enriched.get("title") or entry.title
            entry.content = enriched.get("content") or entry.content
            entry.mood = enriched.get("mood") or entry.mood
            entry.aid = enriched.get("aid") or entry.aid
            entry.save()

        session.status = ChatSession.Status.FINALIZED
        session.journal_entry = entry
        session.messages = transcript
        session.save()

        refresh_bio(request.user)
        refresh_day_streak(request.user)
        return Response(
            {
                "entry": JournalEntrySerializer(entry).data,
                "finalized": True,
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
