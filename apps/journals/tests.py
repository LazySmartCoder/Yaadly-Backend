from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile

from .models import ChatSession, Gallery, JournalEntry


class JournalEntryAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # Never call the real Gemini API during tests.
        self._journalize_patch = patch("apps.journals.views.journalize", return_value=None)
        self._journalize_patch.start()
        self._followup_patch = patch("apps.journals.views.ask_followup", return_value=None)
        self._followup_patch.start()

    def tearDown(self):
        self._followup_patch.stop()
        self._journalize_patch.stop()

    def _get(self, *args, **kwargs):
        # The LanAwareSSLRedirectMiddleware only redirects public hostnames;
        # a localhost Host header keeps tests on plain HTTP like a LAN device.
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def test_list_entries(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-02", title="Hello", content="World"
        )
        response = self._get(reverse("entry-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_create_entry_sets_owner(self):
        response = self._post(
            reverse("entry-list"),
            {"date": "2026-08-02", "title": "Day one", "content": "Notes"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(JournalEntry.objects.get().user, self.user)

    def test_create_entry_journalizes_with_gemini(self):
        enriched = {
            "title": "Lunch with family",
            "content": "Today was really amazing as we had lunch together.",
            "mood": "Joyful",
        }
        with patch("apps.journals.views.journalize", return_value=enriched):
            response = self._post(
                reverse("entry-list"),
                {"date": "2026-08-05", "content": "today we had lunch together"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        entry = JournalEntry.objects.get()
        self.assertEqual(entry.title, "Lunch with family")
        self.assertEqual(entry.content, "Today was really amazing as we had lunch together.")
        self.assertEqual(entry.mood, "Joyful")

    def test_create_entry_derives_title_when_gemini_unavailable(self):
        response = self._post(
            reverse("entry-list"),
            {"date": "2026-08-05", "content": "today we had lunch together"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        entry = JournalEntry.objects.get()
        self.assertEqual(entry.title, "today we had lunch together")
        self.assertEqual(entry.content, "today we had lunch together")
        self.assertEqual(entry.mood, "")

    def test_create_entry_refreshes_profile_bio(self):
        with patch(
            "apps.journals.views.build_bio",
            return_value=(
                "A quiet observer who finds meaning in small moments.\n"
                "Family and good food are what matter most."
            ),
        ):
            response = self._post(
                reverse("entry-list"),
                {"date": "2026-08-05", "content": "today we had lunch together"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.assertIn("quiet observer", profile.bio)

    def test_bio_failure_keeps_existing_bio(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.bio = "Old bio that must stay."
        profile.save()
        with patch("apps.journals.views.build_bio", return_value=None):
            response = self._post(
                reverse("entry-list"),
                {"date": "2026-08-05", "content": "some entry"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        profile.refresh_from_db()
        self.assertEqual(profile.bio, "Old bio that must stay.")

    def test_entries_are_owner_scoped(self):
        other = get_user_model().objects.create_user(username="bob", password="secret123")
        JournalEntry.objects.create(
            user=other, date="2026-08-02", title="Private", content="Secret"
        )
        response = self._get(reverse("entry-list"))
        self.assertEqual(response.data["count"], 0)

    def test_search_queries_the_database(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-02", title="Beach day", content="Sun and sand"
        )
        JournalEntry.objects.create(
            user=self.user, date="2026-08-03", title="Work", content="Meetings all day"
        )
        response = self._get(reverse("entry-list") + "?search=beach")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["title"], "Beach day")

    def test_search_matches_content_too(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-02", title="Long walk", content="The hills were quiet"
        )
        response = self._get(reverse("entry-list") + "?search=quiet")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["title"], "Long walk")

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._get(reverse("entry-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_followup_returns_question(self):
        with patch(
            "apps.journals.views.ask_followup",
            return_value="What made today special?",
        ):
            response = self._post(
                reverse("followup"),
                {"content": "today we had lunch together with my family"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["question"], "What made today special?")

    def test_followup_with_empty_content(self):
        response = self._post(reverse("followup"), {"content": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["question"], "")

    def test_followup_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._post(
            reverse("followup"),
            {"content": "some words about my day"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


class ChatEodTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="eod", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        pass

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def _transcript(self):
        return [
            {"role": "user", "content": "Had a rough day at work."},
            {"role": "assistant", "content": "That sounds heavy. What happened?"},
            {"role": "user", "content": "My manager moved my project to Friday."},
        ]

    def test_eod_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._post(
            reverse("chat-eod"),
            {"date": _days_ago(1), "messages": self._transcript()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_eod_requires_date_and_messages(self):
        response = self._post(reverse("chat-eod"), {"messages": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self._post(
            reverse("chat-eod"), {"date": _days_ago(1)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_eod_rejects_bad_and_future_dates(self):
        response = self._post(
            reverse("chat-eod"),
            {"date": "not-a-date", "messages": self._transcript()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        future = (date.today() + timedelta(days=1)).isoformat()
        response = self._post(
            reverse("chat-eod"),
            {"date": future, "messages": self._transcript()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_eod_rejects_transcript_without_user_messages(self):
        response = self._post(
            reverse("chat-eod"),
            {
                "date": _days_ago(1),
                "messages": [{"role": "assistant", "content": "Hi!"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_eod_creates_journal_entry_from_chat(self):
        enriched = {
            "title": "A heavy day",
            "content": "I told Yaadly that my manager moved my project.",
            "mood": "Anxious",
            "aid": "A heavy work day shared with Yaadly.",
        }
        with patch("apps.journals.views.journalize_chat", return_value=enriched):
            response = self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["finalized"])
        entry = JournalEntry.objects.get()
        self.assertEqual(entry.date, date.today() - timedelta(days=1))
        self.assertEqual(entry.type, JournalEntry.EntryType.TEXT)
        self.assertEqual(entry.title, "A heavy day")
        self.assertEqual(entry.content, "I told Yaadly that my manager moved my project.")
        self.assertEqual(entry.mood, "Anxious")
        self.assertEqual(entry.user, self.user)

    def test_eod_is_idempotent(self):
        enriched = {
            "title": "A heavy day",
            "content": "Today was heavy.",
            "mood": "Anxious",
            "aid": "A heavy day.",
        }
        with patch(
            "apps.journals.views.journalize_chat", return_value=enriched
        ) as mocked:
            first = self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
            second = self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(ChatSession.objects.count(), 1)
        self.assertEqual(second.data["entry"]["id"], first.data["entry"]["id"])

    def test_eod_stays_pending_when_gemini_unavailable(self):
        with patch("apps.journals.views.journalize_chat", return_value=None):
            response = self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["finalized"])
        self.assertEqual(JournalEntry.objects.count(), 0)
        session = ChatSession.objects.get()
        self.assertEqual(session.status, ChatSession.Status.PENDING)

    def test_eod_entry_is_owner_scoped(self):
        other = get_user_model().objects.create_user(username="eod2", password="secret123")
        with patch(
            "apps.journals.views.journalize_chat",
            return_value={"title": "T", "content": "C", "mood": "M", "aid": "A"},
        ):
            self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
        self.client.force_authenticate(user=other)
        with patch("apps.journals.views.journalize_chat", return_value=None):
            response = self._post(
                reverse("chat-eod"),
                {"date": _days_ago(1), "messages": self._transcript()},
                format="json",
            )
        # The other user's identical day stays pending (their own session).
        self.assertFalse(response.data["finalized"])


class DayStreakTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="streak", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self._journalize_patch = patch("apps.journals.views.journalize", return_value=None)
        self._journalize_patch.start()
        self._followup_patch = patch("apps.journals.views.ask_followup", return_value=None)
        self._followup_patch.start()

    def tearDown(self):
        self._followup_patch.stop()
        self._journalize_patch.stop()

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def _delete(self, *args, **kwargs):
        return self.client.delete(*args, HTTP_HOST="localhost", **kwargs)

    def _create_entry(self, date_str, content="some entry"):
        response = self._post(
            reverse("entry-list"),
            {"date": date_str, "content": content},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_first_entry_today_starts_streak_at_one(self):
        self._create_entry(date.today().isoformat())
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.day_streak, 1)

    def test_streak_adds_one_for_each_consecutive_day(self):
        self._create_entry(_days_ago(2))
        self._create_entry(_days_ago(1))
        self._create_entry(date.today().isoformat())
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.day_streak, 3)

    def test_streak_resets_to_zero_when_a_day_is_missed(self):
        self._create_entry(_days_ago(3))
        self._create_entry(_days_ago(2))
        self._create_entry(_days_ago(1))
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.day_streak, 0)

    def test_missing_today_but_written_yesterday_is_zero(self):
        self._create_entry(_days_ago(1))
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.day_streak, 0)

    def test_deleting_entry_recomputes_streak(self):
        self._create_entry(_days_ago(1))
        self._create_entry(date.today().isoformat())
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.day_streak, 2)
        entry = JournalEntry.objects.get(date=date.today())
        response = self._delete(reverse("entry-detail", args=[entry.id]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        profile.refresh_from_db()
        self.assertEqual(profile.day_streak, 0)

    def test_profile_serializer_returns_day_streak(self):
        self._create_entry(date.today().isoformat())
        response = self._get(reverse("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["day_streak"], 1)

    def _get(self, *args, **kwargs):
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)


class StatsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="stats", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self._journalize_patch = patch("apps.journals.views.journalize", return_value=None)
        self._journalize_patch.start()
        self._followup_patch = patch("apps.journals.views.ask_followup", return_value=None)
        self._followup_patch.start()

    def tearDown(self):
        self._followup_patch.stop()
        self._journalize_patch.stop()

    def _get(self, *args, **kwargs):
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def test_stats_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._get(reverse("stats"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_stats_counts_entries_created_this_month_only(self):
        today = date.today()
        response = self._post(
            reverse("entry-list"),
            {"date": today.isoformat(), "content": "current month"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        past = today.replace(day=1)
        if past.month == 1:
            past = today.replace(year=today.year - 1, month=12, day=28)
        else:
            past = today.replace(month=past.month - 1, day=28)

        old = JournalEntry.objects.create(
            user=self.user,
            date=past,
            title="Old",
            content="Last month",
        )
        JournalEntry.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )

        response = self._get(reverse("stats"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["entries_this_month"], 1)

    def test_stats_counts_total_photos(self):
        self._post(
            reverse("entry-list"),
            {"date": date.today().isoformat(), "content": "with photos"},
            format="json",
        )
        entry = JournalEntry.objects.get(user=self.user)
        Gallery.objects.create(user=self.user, entry=entry, url="https://bucket.example.com/a.jpg")
        Gallery.objects.create(user=self.user, entry=entry, url="https://bucket.example.com/b.jpg")

        response = self._get(reverse("stats"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_photos"], 2)
        self.assertEqual(response.data["total_entries"], 1)


class TodayDropsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="drops", password="secret123")
        self.client = APIClient()

    def _get(self, *args, **kwargs):
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)

    def test_today_drops_is_public(self):
        response = self._get(reverse("today-drops"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_today_drops_counts_all_users_entries_created_today(self):
        other = get_user_model().objects.create_user(username="drops2", password="secret123")
        today = timezone.localdate()
        JournalEntry.objects.create(
            user=self.user, date=today, title="Mine", content="Today"
        )
        JournalEntry.objects.create(
            user=other, date=today, title="Theirs", content="Today"
        )

        old = JournalEntry.objects.create(
            user=self.user,
            date=today - timedelta(days=1),
            title="Old",
            content="Yesterday",
        )
        JournalEntry.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        response = self._get(reverse("today-drops"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)


class ChatTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="chat", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def test_chat_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._post(reverse("chat"), {"messages": [{"role": "user", "content": "hi"}]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_chat_requires_messages(self):
        response = self._post(reverse("chat"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self._post(reverse("chat"), {"messages": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_chat_returns_reply(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-06", title="Day", content="We hiked the hills together."
        )
        with patch(
            "apps.journals.views.route_chat",
            return_value={"intent": "general", "date": None, "question": None},
        ), patch(
            "apps.journals.views.chat_reply", return_value="What a lovely memory."
        ) as mocked:
            response = self._post(
                reverse("chat"),
                {"messages": [{"role": "user", "content": "It's been a rough week."}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reply"], "What a lovely memory.")
        # Everyday chat must NOT surface journal entries.
        self.assertIsNone(mocked.call_args.kwargs["memories"])

    def test_chat_recall_returns_entry(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-06", title="Hills", content="We hiked the hills together."
        )
        with patch(
            "apps.journals.views.route_chat",
            return_value={"intent": "detail", "date": "2026-08-06", "question": None},
        ), patch(
            "apps.journals.views.chat_reply", return_value="That hike sounded lovely."
        ) as mocked:
            response = self._post(
                reverse("chat"),
                {"messages": [{"role": "user", "content": "What did I write on Aug 6?"}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reply"], "That hike sounded lovely.")
        self.assertIn("We hiked the hills together.", mocked.call_args.args[1])

    def test_chat_asks_for_date(self):
        JournalEntry.objects.create(
            user=self.user, date="2026-08-06", title="Day", content="We hiked the hills together."
        )
        with patch(
            "apps.journals.views.route_chat",
            return_value={
                "intent": "ask_date",
                "date": "2026-08-06",
                "question": "Are you talking about August 6?",
            },
        ), patch("apps.journals.views.chat_reply") as mocked:
            response = self._post(
                reverse("chat"),
                {"messages": [{"role": "user", "content": "Remember that day we hiked?"}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reply"], "Are you talking about August 6?")
        mocked.assert_not_called()

    def test_chat_stays_present_without_entries(self):
        with patch(
            "apps.journals.views.chat_reply", return_value="Tell me more about it."
        ) as mocked:
            response = self._post(
                reverse("chat"),
                {"messages": [{"role": "user", "content": "I'm feeling a bit low today."}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reply"], "Tell me more about it.")
        self.assertIsNone(mocked.call_args.kwargs["memories"])

    def test_chat_falls_back_when_model_unavailable(self):
        with patch("apps.journals.views.chat_reply", return_value=None):
            response = self._post(
                reverse("chat"),
                {"messages": [{"role": "user", "content": "hi"}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["reply"])


def _fake_image(name="photo.jpg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0fakephotojpeg", content_type="image/jpeg")


class GalleryAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="gallery", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.entry = JournalEntry.objects.create(
            user=self.user, date="2026-08-06", title="Day", content="Notes"
        )
        self.url = reverse("entry-photos", args=[self.entry.id])

    def _get(self, *args, **kwargs):
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)

    def _post(self, *args, **kwargs):
        return self.client.post(*args, HTTP_HOST="localhost", **kwargs)

    def _delete(self, *args, **kwargs):
        return self.client.delete(*args, HTTP_HOST="localhost", **kwargs)

    def test_upload_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._post(self.url, {"photos": _fake_image()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_upload_requires_at_least_one_photo(self):
        response = self._post(self.url, {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Gallery.objects.count(), 0)

    def test_upload_creates_gallery_rows_with_bucket_url(self):
        with patch("apps.journals.views.default_storage") as storage:
            storage.save.side_effect = lambda name, content: name
            storage.url.side_effect = lambda name: f"https://bucket.example.com/{name}"
            response = self._post(
                self.url,
                {"photos": [_fake_image("a.jpg"), _fake_image("b.png")]},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(Gallery.objects.count(), 2)
        for photo in Gallery.objects.all():
            self.assertEqual(photo.entry, self.entry)
            self.assertTrue(photo.url.startswith("https://bucket.example.com/gallery/"))
            self.assertTrue(photo.url.endswith(".jpg") or photo.url.endswith(".png"))

    def test_upload_rejects_another_users_entry(self):
        other = get_user_model().objects.create_user(username="mario", password="secret123")
        other_entry = JournalEntry.objects.create(
            user=other, date="2026-08-06", title="Private", content="Secret"
        )
        with patch("apps.journals.views.default_storage"):
            response = self._post(
                reverse("entry-photos", args=[other_entry.id]),
                {"photos": _fake_image()},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Gallery.objects.count(), 0)

    def test_list_returns_only_that_entries_photos(self):
        other = get_user_model().objects.create_user(username="luigi", password="secret123")
        other_entry = JournalEntry.objects.create(
            user=other, date="2026-08-06", title="Other", content="Hidden"
        )
        Gallery.objects.create(entry=self.entry, url="https://bucket.example.com/one.jpg")
        Gallery.objects.create(entry=self.entry, url="https://bucket.example.com/two.jpg")
        Gallery.objects.create(entry=other_entry, url="https://bucket.example.com/other.jpg")
        response = self._get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_list_for_another_users_entry_is_404(self):
        other = get_user_model().objects.create_user(username="wario", password="secret123")
        other_entry = JournalEntry.objects.create(
            user=other, date="2026-08-06", title="Private", content="Secret"
        )
        response = self._get(reverse("entry-photos", args=[other_entry.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleting_entry_cascades_gallery_rows_and_deletes_files(self):
        Gallery.objects.create(entry=self.entry, url="https://bucket.example.com/gallery/one.jpg")
        with patch("apps.journals.views.default_storage") as storage:
            storage.delete.return_value = None
            response = self._delete(reverse("entry-detail", args=[self.entry.id]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Gallery.objects.count(), 0)
        storage.delete.assert_called_once_with("gallery/one.jpg")

    def test_deleting_entry_without_gallery_files(self):
        response = self._delete(reverse("entry-detail", args=[self.entry.id]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Gallery.objects.count(), 0)
