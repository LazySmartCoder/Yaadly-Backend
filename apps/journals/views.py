from rest_framework import mixins, viewsets

from .models import JournalEntry
from .serializers import JournalEntrySerializer


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
