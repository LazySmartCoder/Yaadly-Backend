from django.conf import settings
from rest_framework import serializers

from .models import JournalEntry


class JournalEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalEntry
        fields = [
            "id",
            "date",
            "title",
            "content",
            "mood",
            "type",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class TranscribeSerializer(serializers.Serializer):
    """Validates the voice clip uploaded for transcription."""

    audio = serializers.FileField()

    def validate_audio(self, value):
        # Multipart clients (and DRF's test client) often default uploaded files
        # to application/octet-stream; only reject explicitly non-audio types.
        content_type = (getattr(value, "content_type", "") or "").lower()
        if content_type and content_type not in {
            "audio/*",
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "application/octet-stream",
        } and not content_type.startswith("audio/"):
            raise serializers.ValidationError("Uploaded file must be an audio file.")
        if value.size > settings.STT_MAX_AUDIO_BYTES:
            raise serializers.ValidationError(
                "Audio clip is too large. Keep it under 60 seconds."
            )
        return value
