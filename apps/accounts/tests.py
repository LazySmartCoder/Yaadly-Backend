import json
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .models import UserProfile
from .services import fetch_google_profile, revoke_google_token

User = get_user_model()


def fake_claims(**overrides):
    claims = {
        "sub": "google-sub-123",
        "email": "gwen@example.com",
        "name": "Gwen Example",
        "picture": "https://example.com/pic.png",
    }
    claims.update(overrides)
    return claims


PEOPLE_FIELDS = {
    "phone_number": "+1-555-0100",
    "birthday": "1990-07-14",
    "gender": "female",
    "addresses": ["123 Main St, Springfield", "456 Oak Ave, Riverton"],
}


class GoogleLoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("google_login")

    @override_settings(GOOGLE_CLIENT_ID="client-1")
    @mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=fake_claims(),
    )
    def test_new_user_is_created_and_gets_tokens(self, _mock):
        response = self.client.post(self.url, {"id_token": "tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        user = User.objects.get(email="gwen@example.com")
        self.assertEqual(user.profile.google_id, "google-sub-123")
        self.assertEqual(user.profile.display_name, "Gwen Example")

    @override_settings(GOOGLE_CLIENT_ID="client-1")
    @mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=fake_claims(),
    )
    def test_existing_google_user_logs_in(self, _mock):
        user = User.objects.create_user(username="gwen@example.com", email="gwen@example.com")
        UserProfile.objects.create(
            user=user, google_id="google-sub-123", display_name="Old Name"
        )
        response = self.client.post(self.url, {"id_token": "tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.profile.display_name, "Gwen Example")

    @override_settings(GOOGLE_CLIENT_ID="client-1")
    @mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=fake_claims(),
    )
    def test_links_to_existing_email_account(self, _mock):
        user = User.objects.create_user(username="gwen@example.com", email="gwen@example.com")
        response = self.client.post(self.url, {"id_token": "tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.profile.google_id, "google-sub-123")

    @override_settings(GOOGLE_CLIENT_ID="client-1")
    @mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        side_effect=ValueError("bad token"),
    )
    def test_invalid_token_rejected(self, _mock):
        response = self.client.post(self.url, {"id_token": "bad"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_token_rejected(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GooglePeopleProfileTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("google_profile")
        self.user = User.objects.create_user(
            username="gwen@example.com", email="gwen@example.com"
        )
        self.profile = UserProfile.objects.create(
            user=self.user, google_id="google-sub-123", display_name="Gwen Example"
        )
        self.client.force_authenticate(user=self.user)

    @mock.patch(
        "apps.accounts.views.fetch_google_profile",
        return_value=PEOPLE_FIELDS,
    )
    def test_stores_people_profile_fields(self, _fetch):
        response = self.client.post(self.url, {"access_token": "access-tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone_number, "+1-555-0100")
        self.assertEqual(self.profile.birthday, "1990-07-14")
        self.assertEqual(self.profile.gender, "female")
        self.assertEqual(
            self.profile.addresses,
            ["123 Main St, Springfield", "456 Oak Ave, Riverton"],
        )

    @mock.patch(
        "apps.accounts.views.fetch_google_profile",
        return_value=PEOPLE_FIELDS,
    )
    def test_people_profile_view_passes_google_id(self, _fetch):
        response = self.client.post(self.url, {"access_token": "access-tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        _fetch.assert_called_once()
        self.assertEqual(_fetch.call_args.kwargs.get("expected_google_id"), "google-sub-123")

    @mock.patch(
        "apps.accounts.views.fetch_google_profile",
        return_value={},
    )
    def test_empty_people_data_leaves_profile_unchanged(self, _fetch):
        response = self.client.post(self.url, {"access_token": "access-tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone_number, "")
        self.assertEqual(self.profile.addresses, [])
        self.assertIn("user", response.data)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.url, {"access_token": "access-tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_access_token_rejected(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch(
        "apps.accounts.views.fetch_google_profile",
        side_effect=RuntimeError("unexpected"),
    )
    def test_people_api_failure_returns_user_not_error(self, _fetch):
        response = self.client.post(self.url, {"access_token": "access-tok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("user", response.data)


class FetchGoogleProfileTests(TestCase):
    """Unit tests for the People API client service."""

    def _mock_response(self, status_code=200, json_data=None):
        response = mock.Mock()
        response.status_code = status_code
        response.text = json.dumps(json_data) if json_data is not None else ""
        response.json.return_value = json_data or {}
        return response

    def test_returns_normalized_fields(self):
        payload = {
            "phoneNumbers": [{"value": "+1-555-0100", "canonicalForm": "+15550100"}],
            "birthdays": [{"date": {"year": 1990, "month": 7, "day": 14}}],
            "genders": [{"value": "female"}],
            "addresses": [
                {"formattedValue": "123 Main St, Springfield", "type": "home"}
            ],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ) as mocked:
            result = fetch_google_profile("access-tok")
        mocked.assert_called_once()
        self.assertEqual(result["phone_number"], "+1-555-0100")
        self.assertEqual(result["birthday"], "1990-07-14")
        self.assertEqual(result["gender"], "female")
        self.assertEqual(result["addresses"], ["123 Main St, Springfield"])

    def test_expected_email_matching_account_keeps_fields(self):
        payload = {
            "emailAddresses": [{"value": "Gwen@Example.com"}],
            "genders": [{"value": "female"}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok", expected_email="gwen@example.com")
        self.assertEqual(result["gender"], "female")

    def test_expected_email_mismatch_returns_empty(self):
        payload = {
            "emailAddresses": [{"value": "other@example.com"}],
            "genders": [{"value": "female"}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok", expected_email="gwen@example.com")
        self.assertEqual(result, {})

    def test_expected_email_missing_from_payload_returns_empty(self):
        payload = {"genders": [{"value": "female"}]}
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok", expected_email="gwen@example.com")
        self.assertEqual(result, {})

    def test_google_id_matching_resource_name_keeps_fields(self):
        payload = {
            "resourceName": "people/123456789",
            "genders": [{"value": "female"}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok", expected_google_id="123456789")
        self.assertEqual(result["gender"], "female")

    def test_google_id_mismatch_returns_empty(self):
        payload = {
            "resourceName": "people/999",
            "genders": [{"value": "female"}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok", expected_google_id="123456789")
        self.assertEqual(result, {})

    def test_google_id_without_resource_name_falls_back_to_email(self):
        payload = {
            "emailAddresses": [{"value": "Gwen@Example.com"}],
            "genders": [{"value": "female"}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile(
                "access-tok",
                expected_google_id="123456789",
                expected_email="gwen@example.com",
            )
        self.assertEqual(result["gender"], "female")

    def test_google_id_mismatch_without_resource_name_returns_empty(self):
        payload = {"genders": [{"value": "female"}]}
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile(
                "access-tok",
                expected_google_id="123456789",
                expected_email="gwen@example.com",
            )
        self.assertEqual(result, {})

    def test_birthday_without_year_is_stored_as_month_day(self):
        payload = {"birthdays": [{"date": {"month": 7, "day": 14}}]}
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok")
        self.assertEqual(result["birthday"], "07-14")

    def test_partial_fields_are_skipped(self):
        payload = {
            "phoneNumbers": [{"value": ""}],
            "genders": [{"value": " "}],
            "addresses": [{"formattedValue": ""}],
        }
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(json_data=payload),
        ):
            result = fetch_google_profile("access-tok")
        self.assertEqual(result, {})

    def test_denied_scopes_return_empty(self):
        with mock.patch(
            "apps.accounts.services.requests.get",
            return_value=self._mock_response(status_code=403),
        ):
            result = fetch_google_profile("access-tok")
        self.assertEqual(result, {})

    def test_network_error_returns_empty(self):
        with mock.patch(
            "apps.accounts.services.requests.get",
            side_effect=requests.RequestException("boom"),
        ):
            result = fetch_google_profile("access-tok")
        self.assertEqual(result, {})

    def test_missing_access_token_returns_empty(self):
        self.assertEqual(fetch_google_profile(None), {})


class DeleteAccountTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("delete_account")
        self.user = User.objects.create_user(
            username="gwen@example.com", email="gwen@example.com"
        )
        self.profile = UserProfile.objects.create(
            user=self.user, google_id="google-sub-123", display_name="Gwen Example"
        )
        self.client.force_authenticate(user=self.user)

    @mock.patch("apps.accounts.views.revoke_google_token", return_value=True)
    def test_deletes_user_and_profile(self, _revoke):
        response = self.client.delete(
            self.url, {"access_token": "access-tok"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("detail", response.data)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(UserProfile.objects.filter(pk=self.profile.pk).exists())
        _revoke.assert_called_once_with("access-tok")

    @mock.patch("apps.accounts.views.revoke_google_token", return_value=False)
    def test_deletes_even_if_google_revoke_fails(self, _revoke):
        response = self.client.delete(
            self.url, {"access_token": "access-tok"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    @mock.patch("apps.accounts.views.revoke_google_token")
    def test_revoke_skipped_without_token(self, _revoke):
        response = self.client.delete(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        _revoke.assert_not_called()
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.delete(
            self.url, {"access_token": "access-tok"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PremiumFieldTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="prem@example.com", email="prem@example.com")
        UserProfile.objects.create(
            user=self.user, google_id="google-sub-prem", display_name="Prem User"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _get(self, *args, **kwargs):
        # localhost Host keeps the LanAwareSSLRedirectMiddleware on plain HTTP.
        return self.client.get(*args, HTTP_HOST="localhost", **kwargs)

    def test_premium_user_defaults_to_false(self):
        response = self._get(reverse("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["user"]["premium_user"], False)

    def test_premium_user_true_when_profile_flag_set(self):
        self.user.profile.premium_user = True
        self.user.profile.save(update_fields=["premium_user", "updated_at"])

        response = self._get(reverse("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["user"]["premium_user"], True)


class RevokeGoogleTokenTests(TestCase):
    def _mock_response(self, status_code=200):
        response = mock.Mock()
        response.status_code = status_code
        response.text = "ok"
        return response

    def test_revokes_token(self):
        with mock.patch(
            "apps.accounts.services.requests.post",
            return_value=self._mock_response(),
        ) as mocked:
            result = revoke_google_token("access-tok")
        self.assertTrue(result)
        mocked.assert_called_once()
        self.assertEqual(
            mocked.call_args.args[0], "https://oauth2.googleapis.com/revoke"
        )

    def test_non_200_returns_false(self):
        with mock.patch(
            "apps.accounts.services.requests.post",
            return_value=self._mock_response(status_code=400),
        ):
            self.assertFalse(revoke_google_token("access-tok"))

    def test_network_error_returns_false(self):
        with mock.patch(
            "apps.accounts.services.requests.post",
            side_effect=requests.RequestException("boom"),
        ):
            self.assertFalse(revoke_google_token("access-tok"))

    def test_missing_token_returns_false(self):
        self.assertFalse(revoke_google_token(None))
        self.assertFalse(revoke_google_token(""))
