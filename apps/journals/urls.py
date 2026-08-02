from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import JournalEntryViewSet, TranscribeView

router = DefaultRouter()
router.register("entries", JournalEntryViewSet, basename="entry")

urlpatterns = [
    path("transcribe/", TranscribeView.as_view(), name="transcribe"),
    *router.urls,
]
