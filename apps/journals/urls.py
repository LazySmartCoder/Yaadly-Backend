from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import JournalEntryViewSet

router = DefaultRouter()
router.register("entries", JournalEntryViewSet, basename="entry")

urlpatterns = [
    *router.urls,
]
