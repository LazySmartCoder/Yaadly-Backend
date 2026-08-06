import uuid
from urllib.parse import urlparse

import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import UserProfile

from .gemini import (
    ask_followup,
    build_bio,
    chat_reply,
    describe_entry,
    journalize,
    route_chat,
    summarize,
)
from .models import Gallery, JournalEntry
from .serializers import GallerySerializer, JournalEntrySerializer
from .transcription import transcribe_audio

logger = logging.getLogger(__name__)


def _one_line(raw_content):
    """Local fallback for AID: a single collapsed line of the raw content."""
    line = " ".join((raw_content or "").split())
    return line[:1000]


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
        self._refresh_day_streak(self.request.user)

    def perform_destroy(self, instance):
        self._delete_gallery_files(instance)
        super().perform_destroy(instance)
        self._refresh_day_streak(self.request.user)

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

    @staticmethod
    def _refresh_day_streak(user):
        """Recompute the user's day streak from their entries and persist it on
        their profile (a missed day resets it to zero)."""
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.recompute_day_streak()


class TranscribeView(APIView):
    def post(self, request):
        audio = request.FILES.get("audio")
        if audio is None:
            return Response(
                {"detail": "An audio file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        text = transcribe_audio(audio.read())
        payload = {"text": text}

        # Persist the original recording to object storage when configured.
        # Best-effort: storage failure must not sink the transcription.
        if settings.USE_S3_STORAGE:
            try:
                name = f"recordings/{uuid.uuid4().hex}.wav"
                audio.seek(0)
                stored = default_storage.save(
                    name,
                    ContentFile(audio.read(), name=name),
                )
                payload["audio_url"] = default_storage.url(stored)
            except Exception:
                payload["audio_url"] = ""

        return Response(payload)


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
    if bucket and key.startswith(bucket + "/"):
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
            }
        )


class ChatView(APIView):
    """Carry on a warm, memory-oriented conversation with the user.

    Each turn the client sends the full conversation; the backend builds a
    compact index of every journal day (date + one-line AID) and routes the
    message: general chit-chat answers straight from that index, references to
    a specific day first confirm the date, and confirmed days pull the full
    title + content before answering. Routing and replies both use the cheap
    flash-lite model. Best-effort: any failure falls back to a gentle reply.
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
            return Response(
                {
                    "reply": "You haven't written any memories yet. "
                    "When you do, I'd love to revisit them with you."
                }
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
                reply = chat_reply(messages, index)
        elif intent == "ask_date":
            question = (route or {}).get("question")
            reply = question or (
                "Hmm, which day are you thinking of? Tell me the date "
                "and I'll revisit it with you."
            )
        else:
            reply = chat_reply(messages, index)

        if not reply:
            return Response(
                {"reply": "I'm always here to listen. Tell me a memory."}
            )
        return Response({"reply": reply})


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
