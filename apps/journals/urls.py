from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ChatView,
    EntryPhotosView,
    FollowUpView,
    JournalEntryViewSet,
    StatsView,
    SummaryView,
    TranscribeView,
)

router = DefaultRouter()
router.register("entries", JournalEntryViewSet, basename="entry")

urlpatterns = [
    path("transcribe/", TranscribeView.as_view(), name="transcribe"),
    path("followup/", FollowUpView.as_view(), name="followup"),
    path("summary/", SummaryView.as_view(), name="summary"),
    path("stats/", StatsView.as_view(), name="stats"),
    path("chat/", ChatView.as_view(), name="chat"),
    path(
        "entries/<uuid:entry_id>/photos/",
        EntryPhotosView.as_view(),
        name="entry-photos",
    ),
    *router.urls,
]
