import json
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .models import JournalEntry
from .services import (
    TranscriptionError,
    TranscriptionUnavailable,
    transcribe,
)


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


class TranscribeAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice", password="secret123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = reverse("transcribe")

    def _wav(self, name="voice.wav"):
        return BytesIO(b"RIFF-fake-pcm-data-for-tests")

    def test_returns_transcript(self):
        with mock.patch(
            "apps.journals.views.transcribe", return_value="hello world"
        ) as mocked:
            response = self.client.post(
                self.url,
                {"audio": self._wav()},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"text": "hello world"})
        mocked.assert_called_once()

    def test_unconfigured_service_returns_503(self):
        with mock.patch(
            "apps.journals.views.transcribe",
            side_effect=TranscriptionUnavailable("not configured"),
        ):
            response = self.client.post(
                self.url, {"audio": self._wav()}, format="multipart"
            )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_google_error_returns_502(self):
        with mock.patch(
            "apps.journals.views.transcribe",
            side_effect=TranscriptionError("boom"),
        ):
            response = self.client.post(
                self.url, {"audio": self._wav()}, format="multipart"
            )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("boom", response.data["detail"])

    def test_rejects_non_audio_file(self):
        response = self.client.post(
            self.url,
            {
                "audio": SimpleUploadedFile(
                    "notes.txt", b"not-audio", content_type="text/plain"
                )
            },
            format="multipart",
            HTTP_CONTENT_TYPE="multipart/form-data",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.url, {"audio": self._wav()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TranscribeServiceTests(TestCase):
    """Unit tests for the Google Cloud Speech-to-Text client service."""

    def _mock_speech_response(self, transcripts):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "results": [
                {"alternatives": [{"transcript": transcript}]}
                for transcript in transcripts
            ]
        }
        return response

    @override_settings(
        GOOGLE_SPEECH_CREDENTIALS_JSON=json.dumps(
            {
                "type": "service_account",
                "project_id": "proj",
                "private_key_id": "k",
                "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                "client_email": "svc@proj.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        STT_LANGUAGE_CODE="en-US",
    )
    @mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_info"
    )
    def test_returns_joined_transcript(self, _from_info):
        creds = mock.Mock()
        creds.token = "tok"
        _from_info.return_value = creds
        with mock.patch(
            "apps.journals.services.requests.post",
            return_value=self._mock_speech_response(["one", "two"]),
        ) as mocked:
            text = transcribe(b"fake-audio")
        self.assertEqual(text, "one two")
        payload = json.loads(mocked.call_args.kwargs["data"])
        self.assertEqual(payload["config"]["encoding"], "LINEAR16")
        self.assertEqual(payload["config"]["sampleRateHertz"], 16000)
        self.assertEqual(payload["config"]["languageCode"], "en-US")
        self.assertTrue(payload["config"]["enableAutomaticPunctuation"])
        self.assertEqual(
            mocked.call_args.kwargs["headers"]["Authorization"], "Bearer tok"
        )

    @override_settings(
        GOOGLE_SPEECH_CREDENTIALS_JSON="{}",
        STT_LANGUAGE_CODE="en-US",
    )
    def test_unconfigured_raises_unavailable(self):
        with self.assertRaises(TranscriptionUnavailable):
            transcribe(b"fake-audio")

    @override_settings(
        GOOGLE_SPEECH_CREDENTIALS_JSON=json.dumps(
            {
                "type": "service_account",
                "project_id": "proj",
                "private_key_id": "k",
                "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                "client_email": "svc@proj.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        STT_LANGUAGE_CODE="en-US",
    )
    @mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_info"
    )
    def test_no_speech_raises_error(self, _from_info):
        creds = mock.Mock()
        creds.token = "tok"
        _from_info.return_value = creds
        with mock.patch(
            "apps.journals.services.requests.post",
            return_value=self._mock_speech_response([]),
        ):
            with self.assertRaises(TranscriptionError):
                transcribe(b"fake-audio")

    @override_settings(
        GOOGLE_SPEECH_CREDENTIALS_JSON=json.dumps(
            {
                "type": "service_account",
                "project_id": "proj",
                "private_key_id": "k",
                "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                "client_email": "svc@proj.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        STT_LANGUAGE_CODE="en-US",
    )
    @mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_info"
    )
    def test_google_error_raises(self, _from_info):
        creds = mock.Mock()
        creds.token = "tok"
        _from_info.return_value = creds
        response = mock.Mock()
        response.status_code = 403
        response.text = '{"error": {"message": "permission denied"}}'
        response.json.return_value = {
            "error": {"message": "permission denied"}
        }
        with mock.patch(
            "apps.journals.services.requests.post",
            return_value=response,
        ):
            with self.assertRaises(TranscriptionError) as raised:
                transcribe(b"fake-audio")
        self.assertIn("permission denied", str(raised.exception))
