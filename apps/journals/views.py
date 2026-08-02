from rest_framework import mixins, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .models import JournalEntry
from .serializers import JournalEntrySerializer, TranscribeSerializer
from .services import (
    TranscriptionError,
    TranscriptionUnavailable,
    transcribe,
)


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
        serializer.save(user=self.request.user)


class TranscribeView(APIView):
    """Transcribe an uploaded voice clip with Google Cloud Speech-to-Text.

    Accepts a multipart `audio` field (16 kHz mono PCM WAV) and returns the
    recognised text so the client never talks to Google directly and the
    credentials stay server-side.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = TranscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        audio = serializer.validated_data["audio"]

        try:
            text = transcribe(audio.read())
        except TranscriptionUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except TranscriptionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"text": text})
