from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .models import JournalEntry


class JournalEntryAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_list_entries(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-02", title="Hello", content="World"
        )
        response = self.client.get(reverse("entry-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_create_entry_sets_owner(self):
        response = self.client.post(
            reverse("entry-list"),
            {"date": "2026-08-02", "title": "Day one", "content": "Notes"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(JournalEntry.objects.get().user, self.user)

    def test_entries_are_owner_scoped(self):
        other = get_user_model().objects.create_user(username="bob", password="secret123")
        JournalEntry.objects.create(
            user=other, date="2026-08-02", title="Private", content="Secret"
        )
        response = self.client.get(reverse("entry-list"))
        self.assertEqual(response.data["count"], 0)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("entry-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
