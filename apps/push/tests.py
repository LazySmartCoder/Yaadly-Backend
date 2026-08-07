import json
from unittest import mock
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.journals.models import JournalEntry

from .gemini import _clean_body, _fit_line
from .models import DeviceToken, PushLog
from .services import DeviceNotRegistered, FcmError

User = get_user_model()


class DeviceTokenRegistrationTests(TestCase):
    def setUp(self):
        # LanAwareSSLRedirectMiddleware only redirects public hostnames; tests
        # hit "testserver" so every request must pretend to be local.
        self.client = APIClient(HTTP_HOST="localhost")
        self.url = reverse("register_device_token")
        self.user = User.objects.create_user(username="a@example.com", email="a@example.com")

    def test_requires_authentication(self):
        response = self.client.post(
            self.url, {"token": "tok-1"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_registers_a_token(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            self.url,
            {"token": "tok-1", "platform": "android"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token = DeviceToken.objects.get(token="tok-1")
        self.assertEqual(token.user, self.user)
        self.assertEqual(token.platform, "android")
        self.assertTrue(token.is_active)

    def test_re_registration_is_idempotent(self):
        self.client.force_authenticate(self.user)
        for _ in range(2):
            response = self.client.post(
                self.url, {"token": "tok-1"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(DeviceToken.objects.filter(token="tok-1").count(), 1)

    def test_token_is_reassigned_to_new_owner(self):
        other = User.objects.create_user(username="b@example.com", email="b@example.com")
        DeviceToken.objects.create(user=self.user, token="tok-1")
        self.client.force_authenticate(other)
        response = self.client.post(
            self.url, {"token": "tok-1", "platform": "ios"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token = DeviceToken.objects.get(token="tok-1")
        self.assertEqual(token.user, other)
        self.assertEqual(token.platform, "ios")


class GeminiHelpersTests(TestCase):
    def test_fit_line_truncates(self):
        self.assertEqual(_fit_line("a" * 200, 10), "aaaaaaaaa…")
        self.assertEqual(len(_fit_line("a" * 200, 10)), 10)

    def test_clean_body_enforces_two_lines(self):
        body = _clean_body("line one\nline two\nline three\nline four")
        self.assertEqual(len(body), 2)
        self.assertEqual(body, ["line one", "line two"])

    def test_clean_body_collapses_whitespace(self):
        body = _clean_body("  hello   world  \n\n second  ")
        self.assertEqual(body, ["hello world", "second"])


class ServicesTests(TestCase):
    def test_empty_token_raises_device_not_registered(self):
        with self.assertRaises(DeviceNotRegistered):
            from .services import send_message

            send_message("", "title", "body")

    def test_not_configured_raises(self):
        with mock.patch("apps.push.services._load_credentials", return_value=None):
            from .services import send_message

            with self.assertRaises(FcmError):
                send_message("tok", "title", "body")

    def _configured_send(self):
        creds = mock.Mock(valid=True, token="access-token", project_id="p1")
        patches = [
            mock.patch("apps.push.services._load_credentials", return_value=creds),
            mock.patch("apps.push.services._access_token", return_value="access-token"),
            mock.patch("apps.push.services.project_id", return_value="p1"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_payload_uses_high_priority_channel(self):
        from .services import send_message

        self._configured_send()
        with mock.patch("apps.push.services.requests.post") as post:
            post.return_value = mock.Mock(status_code=200, content=b"{}", text="{}")
            send_message("tok-1", "title", "body")
        payload = post.call_args.kwargs["json"]["message"]
        self.assertEqual(payload["android"]["priority"], "HIGH")
        self.assertEqual(
            payload["android"]["notification"]["channel_id"], "yaadly_messages"
        )

    def test_transient_failure_is_retried_then_succeeds(self):
        from .services import send_message

        self._configured_send()
        fail = mock.Mock(status_code=503, content=b"", text="unavailable")
        ok = mock.Mock(status_code=200, content=b"{}", text="{}")
        with mock.patch("apps.push.services.requests.post", side_effect=[fail, ok]) as post:
            send_message("tok-1", "title", "body")
        self.assertEqual(post.call_count, 2)

    def test_transient_failure_then_unregistered_raises_device_not_registered(self):
        from .services import send_message

        self._configured_send()
        fail = mock.Mock(status_code=503, content=b"", text="unavailable")
        unregistered = mock.Mock(
            status_code=400, content=b"", text='{"error": {"status": "UNREGISTERED"}}'
        )
        with mock.patch(
            "apps.push.services.requests.post",
            side_effect=[fail, unregistered],
        ) as post:
            with self.assertRaises(DeviceNotRegistered):
                send_message("tok-1", "title", "body")
        self.assertEqual(post.call_count, 2)

    def test_exhausted_retries_raise_fcm_error(self):
        from .services import send_message

        self._configured_send()
        fail = mock.Mock(status_code=503, content=b"", text="unavailable")
        with mock.patch("apps.push.services.requests.post", return_value=fail) as post:
            with self.assertRaises(FcmError):
                send_message("tok-1", "title", "body")
        self.assertEqual(post.call_count, 3)


class DeactivateDeviceTokenTests(TestCase):
    def setUp(self):
        self.client = APIClient(HTTP_HOST="localhost")
        self.url = reverse("deactivate_device_token")
        self.user = User.objects.create_user(username="a@example.com", email="a@example.com")
        self.other = User.objects.create_user(username="b@example.com", email="b@example.com")

    def test_requires_authentication(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivates_all_own_tokens(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        DeviceToken.objects.create(user=self.user, token="tok-2")
        DeviceToken.objects.create(user=self.other, token="tok-other")
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(DeviceToken.objects.get(token="tok-1").is_active)
        self.assertFalse(DeviceToken.objects.get(token="tok-2").is_active)
        self.assertTrue(DeviceToken.objects.get(token="tok-other").is_active)

    def test_deactivates_only_requested_token(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        DeviceToken.objects.create(user=self.user, token="tok-2")
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {"token": "tok-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(DeviceToken.objects.get(token="tok-1").is_active)
        self.assertTrue(DeviceToken.objects.get(token="tok-2").is_active)

    def test_cannot_deactivate_another_users_token(self):
        DeviceToken.objects.create(user=self.other, token="tok-other")
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {"token": "tok-other"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(DeviceToken.objects.get(token="tok-other").is_active)


class SendDailyNotificationsCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u@example.com", email="u@example.com")
        self.token = DeviceToken.objects.create(user=self.user, token="tok-1")
        today = timezone.localdate()
        JournalEntry.objects.create(
            user=self.user,
            date=today - timezone.timedelta(days=2),
            title="A past win",
            content="I aced my interview and my friends cheered for me.",
            mood="Proud",
        )
        JournalEntry.objects.create(
            user=self.user,
            date=today,
            title="Today",
            content="A quiet day.",
            mood="Calm",
        )
        self.fcm_configured = mock.patch(
            "apps.push.management.commands.send_daily_notifications.is_configured",
            return_value=True,
        )
        self.fcm_configured.start()
        self.addCleanup(self.fcm_configured.stop)

    @staticmethod
    def _mock_copy():
        return mock.patch(
            "apps.push.management.commands.send_daily_notifications.generate_for",
            return_value={
                "title": "Everyone believes in you",
                "body": "Everyone believes in you.\nLike the time your friends cheered.",
            },
        )

    def test_command_sends_and_logs(self):
        with self._mock_copy(), mock.patch(
            "apps.push.management.commands.send_daily_notifications.send_message"
        ) as send:
            call_command(
                "send_daily_notifications",
                slot="morning",
                batch_size=10,
            )
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args[0], "tok-1")
        self.assertIn("believe", (args[1] + args[2]).lower())
        self.assertTrue(PushLog.objects.filter(user=self.user, slot="morning").exists())

    def test_slot_runs_once_per_user_per_day(self):
        with self._mock_copy(), mock.patch(
            "apps.push.management.commands.send_daily_notifications.send_message"
        ):
            call_command("send_daily_notifications", slot="evening", batch_size=10)
            call_command("send_daily_notifications", slot="evening", batch_size=10)
        self.assertEqual(
            PushLog.objects.filter(user=self.user, slot="evening").count(),
            1,
        )

    def test_unregistered_token_is_deactivated(self):
        with self._mock_copy(), mock.patch(
            "apps.push.management.commands.send_daily_notifications.send_message",
            side_effect=DeviceNotRegistered("unregistered"),
        ):
            call_command("send_daily_notifications", slot="afternoon", batch_size=10)
        self.refresh_from_db_token()
        self.assertFalse(self.token.is_active)
        self.assertFalse(
            PushLog.objects.filter(user=self.user, slot="afternoon").exists()
        )

    def test_dry_run_sends_nothing_and_logs_nothing(self):
        with self._mock_copy(), mock.patch(
            "apps.push.management.commands.send_daily_notifications.send_message"
        ) as send:
            call_command(
                "send_daily_notifications",
                slot="morning",
                batch_size=10,
                dry_run=True,
            )
        send.assert_not_called()
        self.assertFalse(PushLog.objects.filter(user=self.user, slot="morning").exists())

    def refresh_from_db_token(self):
        self.token.refresh_from_db()


class GeminiModuleTests(TestCase):
    @mock.patch("apps.push.gemini._call_gemini", return_value=None)
    def test_fallback_messages_are_two_lines(self, _mock):
        from .gemini import morning_message, afternoon_memory, evening_message

        self.assertEqual(len(morning_message("x")["body"].splitlines()), 2)
        self.assertEqual(len(afternoon_memory(None)["body"].splitlines()), 2)
        self.assertEqual(len(evening_message("x")["body"].splitlines()), 2)

    @mock.patch(
        "apps.push.gemini._call_gemini",
        return_value=json.dumps(
            {"title": "Believe", "body": "Everyone believes in you.\nLike last week."}
        ),
    )
    def test_ai_messages_are_parsed_to_two_lines(self, _mock):
        from .gemini import morning_message

        result = morning_message("x")
        self.assertEqual(result["title"], "Believe")
        self.assertEqual(result["body"], "Everyone believes in you.\nLike last week.")
