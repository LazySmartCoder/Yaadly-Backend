from datetime import date
from unittest import mock

import os
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.journals.models import Gallery, JournalEntry

from .admin_actions import send_remember_pushes
from .models import DeviceToken
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


class ServicesTests(TestCase):
    def test_service_account_path_is_absolute(self):
        from django.conf import settings as django_settings

        path = django_settings.FCM_SERVICE_ACCOUNT_PATH
        self.assertTrue(os.path.isabs(path))

    def test_find_service_account_file_prefers_hinted_names(self):
        import os as os_module
        import tempfile

        from apps.push import services

        with tempfile.TemporaryDirectory() as tmp:
            other = os_module.path.join(tmp, "plain.json")
            hinted = os_module.path.join(tmp, "my-firebase-service-account.json")
            open(other, "w").close()
            open(hinted, "w").close()
            with self.settings(BASE_DIR=tmp):
                found = services._find_service_account_file()
        self.assertEqual(
            os_module.path.normpath(found), os_module.path.normpath(hinted)
        )

    def test_find_service_account_file_returns_none_without_matches(self):
        import os as os_module
        import tempfile

        from apps.push import services

        with tempfile.TemporaryDirectory() as tmp:
            open(os_module.path.join(tmp, "plain.json"), "w").close()
            with self.settings(BASE_DIR=tmp):
                found = services._find_service_account_file()
        self.assertIsNone(found)

    def test_load_credentials_falls_back_to_autodetected_file(self):
        from apps.push import services

        services._credentials = None
        self.addCleanup(setattr, services, "_credentials", None)
        with self.settings(
            FCM_SERVICE_ACCOUNT_PATH="/nonexistent/creds.json",
            FCM_SERVICE_ACCOUNT_JSON="",
        ):
            with mock.patch(
                "apps.push.services._find_service_account_file",
                return_value="/tmp/creds.json",
            ):
                with mock.patch(
                    "apps.push.services.service_account.Credentials.from_service_account_file"
                ) as from_file:
                    from_file.return_value = mock.Mock()
                    result = services._load_credentials()
        from_file.assert_called_once()
        self.assertEqual(from_file.call_args.args[0], "/tmp/creds.json")
        self.assertIsNotNone(result)

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

    def test_payload_includes_attached_image(self):
        from .services import send_message

        self._configured_send()
        with mock.patch("apps.push.services.requests.post") as post:
            post.return_value = mock.Mock(status_code=200, content=b"{}", text="{}")
            send_message(
                "tok-1",
                "Remember?",
                "Remember this photo from 5 August?",
                data={"type": "remember"},
                image="https://example.com/gallery/a.jpg",
            )
        payload = post.call_args.kwargs["json"]["message"]
        self.assertEqual(
            payload["notification"]["image"], "https://example.com/gallery/a.jpg"
        )
        self.assertEqual(
            payload["data"]["image_url"], "https://example.com/gallery/a.jpg"
        )
        self.assertEqual(payload["data"]["type"], "remember")

    def test_payload_without_image_has_no_image_field(self):
        from .services import send_message

        self._configured_send()
        with mock.patch("apps.push.services.requests.post") as post:
            post.return_value = mock.Mock(status_code=200, content=b"{}", text="{}")
            send_message("tok-1", "title", "body")
        payload = post.call_args.kwargs["json"]["message"]
        self.assertNotIn("image", payload["notification"])
        self.assertNotIn("image_url", payload["data"])


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


class SendRememberPushesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="a@example.com", email="a@example.com"
        )

    def _entry(self, **kwargs):
        return JournalEntry.objects.create(
            user=self.user,
            date=date(2024, 8, 5),
            title=kwargs.pop("title", "Memories"),
            content=kwargs.pop("content", "A sunny day."),
            **kwargs,
        )

    def test_sends_remember_push_with_photo(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        entry = self._entry()
        Gallery.objects.create(
            entry=entry, user=self.user, url="/media/gallery/a.jpg"
        )
        with mock.patch("apps.push.admin_actions.send_message") as send:
            summary = send_remember_pushes()
        self.assertEqual(summary["users_targeted"], 1)
        self.assertEqual(summary["tokens_sent"], 1)
        args, kwargs = send.call_args
        self.assertEqual(args[1], "Remember?")
        self.assertEqual(args[2], "Remember this photo from 5 August?")
        self.assertEqual(kwargs["data"]["type"], "remember")
        self.assertIn("gallery/a.jpg", kwargs["image"])

    def test_sends_to_each_active_token_of_user(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        DeviceToken.objects.create(user=self.user, token="tok-2")
        entry = self._entry()
        Gallery.objects.create(
            entry=entry, user=self.user, url="/media/gallery/a.jpg"
        )
        with mock.patch("apps.push.admin_actions.send_message") as send:
            summary = send_remember_pushes()
        self.assertEqual(summary["users_targeted"], 1)
        self.assertEqual(summary["tokens_sent"], 2)
        self.assertEqual(send.call_count, 2)

    def test_picks_entry_that_has_a_photo(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        self._entry(title="Bare", content="no photo here")
        with_photo = self._entry(title="With photo", content="has a photo")
        Gallery.objects.create(
            entry=with_photo, user=self.user, url="/media/gallery/b.jpg"
        )
        with mock.patch("apps.push.admin_actions.send_message") as send:
            summary = send_remember_pushes()
        self.assertEqual(summary["users_targeted"], 1)
        self.assertIn("gallery/b.jpg", send.call_args[1]["image"])

    def test_skips_user_without_photo_entries(self):
        DeviceToken.objects.create(user=self.user, token="tok-1")
        self._entry()
        with mock.patch("apps.push.admin_actions.send_message") as send:
            summary = send_remember_pushes()
        self.assertEqual(summary["users_skipped"], 1)
        self.assertEqual(summary["users_targeted"], 0)
        send.assert_not_called()

    def test_unregistered_token_is_deactivated(self):
        token = DeviceToken.objects.create(user=self.user, token="tok-1")
        entry = self._entry()
        Gallery.objects.create(
            entry=entry, user=self.user, url="/media/gallery/a.jpg"
        )
        with mock.patch(
            "apps.push.admin_actions.send_message",
            side_effect=DeviceNotRegistered("gone"),
        ):
            summary = send_remember_pushes()
        self.assertEqual(summary["tokens_failed"], 1)
        token.refresh_from_db()
        self.assertFalse(token.is_active)

    def test_inactive_tokens_are_ignored(self):
        DeviceToken.objects.create(user=self.user, token="tok-1", is_active=False)
        entry = self._entry()
        Gallery.objects.create(
            entry=entry, user=self.user, url="/media/gallery/a.jpg"
        )
        with mock.patch("apps.push.admin_actions.send_message") as send:
            summary = send_remember_pushes()
        self.assertEqual(summary["tokens_sent"], 0)
        send.assert_not_called()


class AdminShootTests(TestCase):
    def setUp(self):
        self.client = APIClient(HTTP_HOST="localhost")
        self.url = reverse("admin_shoot")
        self.staff = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="pw",
            is_staff=True,
        )

    def test_requires_staff(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("/admin/login/", response.url)

    def test_admin_index_renders_shoot_button(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "Shoot")
        self.assertContains(response, reverse("admin_shoot"))

    def test_get_is_rejected(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_post_triggers_shoot_and_redirects_to_admin(self):
        self.client.force_login(self.staff)
        summary = {
            "users_targeted": 2,
            "users_skipped": 1,
            "tokens_sent": 3,
            "tokens_failed": 0,
        }
        with mock.patch("apps.push.views.is_configured", return_value=True):
            with mock.patch(
                "apps.push.views.send_remember_pushes", return_value=summary
            ) as shoot:
                response = self.client.post(self.url)
        shoot.assert_called_once_with()
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, reverse("admin:index"))

    def test_post_without_fcm_configuration_skips_shoot(self):
        self.client.force_login(self.staff)
        with mock.patch("apps.push.views.is_configured", return_value=False):
            with mock.patch("apps.push.views.send_remember_pushes") as shoot:
                response = self.client.post(self.url)
        shoot.assert_not_called()
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, reverse("admin:index"))
