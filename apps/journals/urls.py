from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ChatEodView,
    ChatSessionView,
    ChatSessionPhotosView,
    ChatView,
    EntryPhotosView,
    FollowUpView,
    JournalEntryViewSet,
    StatsView,
    SummaryView,
    TodayDropsView,
)

router = DefaultRouter()
router.register("entries", JournalEntryViewSet, basename="entry")

urlpatterns = [
    path("followup/", FollowUpView.as_view(), name="followup"),
    path("summary/", SummaryView.as_view(), name="summary"),
    path("stats/", StatsView.as_view(), name="stats"),
    path("today-drops/", TodayDropsView.as_view(), name="today-drops"),
    path("chat/", ChatView.as_view(), name="chat"),
    path("chat/session/", ChatSessionView.as_view(), name="chat-session"),
    path(
        "chat/session/photos/",
        ChatSessionPhotosView.as_view(),
        name="chat-session-photos",
    ),
    path("chat/eod/", ChatEodView.as_view(), name="chat-eod"),
    path(
        "entries/<uuid:entry_id>/photos/",
        EntryPhotosView.as_view(),
        name="entry-photos",
    ),
    *router.urls,
]
