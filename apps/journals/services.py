import base64
import json
import logging
from pathlib import Path

import requests
from django.conf import settings
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

_SPEECH_URL = "https://speech.googleapis.com/v1/speech:recognize"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# google-auth's default transport has no request timeout; enforce one on the
# credential fetch so a slow Google endpoint cannot hang a request forever.
_CREDENTIALS_TIMEOUT = 15
# The synchronous recognize call can take a while for a 60s clip.
_TRANSCRIBE_TIMEOUT = 90


class TranscriptionUnavailable(Exception):
    """Speech-to-text is not configured on the server."""


class TranscriptionError(Exception):
    """The speech-to-text call failed or returned nothing usable."""


class _TimeoutSession(requests.Session):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, **kwargs)


def transcribe(audio_bytes):
    """Transcribe a 16 kHz mono PCM (WAV) clip with Google Cloud Speech-to-Text.

    Returns the transcribed text. Raises [TranscriptionUnavailable] when the
    server has no Google credentials configured and [TranscriptionError] when
    Google rejects the request or no speech is recognised.
    """
    credentials = _load_credentials()
    token = _access_token(credentials)

    payload = {
        "config": {
            "encoding": "LINEAR16",
            "sampleRateHertz": 16000,
            "languageCode": settings.STT_LANGUAGE_CODE,
            "enableAutomaticPunctuation": True,
            "model": "latest_long",
        },
        "audio": {
            "content": base64.b64encode(audio_bytes).decode("ascii"),
        },
    }

    try:
        response = requests.post(
            _SPEECH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=_TRANSCRIBE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Speech-to-Text request failed: %s", exc)
        raise TranscriptionError(
            "Could not reach the speech service. Please try again."
        ) from exc

    if response.status_code != 200:
        logger.warning(
            "Speech-to-Text error %s: %s", response.status_code, response.text[:500]
        )
        raise TranscriptionError(_speech_error(response))

    results = (response.json() or {}).get("results") or []
    transcripts = [
        alt["transcript"]
        for result in results
        if result.get("alternatives") and (alt := result["alternatives"][0]).get("transcript")
    ]
    text = " ".join(transcripts).strip()
    if not text:
        raise TranscriptionError("No speech was detected. Please try again.")
    return text


def _load_credentials():
    key_file = settings.GOOGLE_SPEECH_CREDENTIALS_FILE
    key_json = settings.GOOGLE_SPEECH_CREDENTIALS_JSON
    try:
        if key_file:
            path = Path(key_file)
            if not path.is_absolute():
                path = settings.BASE_DIR / path
            return service_account.Credentials.from_service_account_file(
                path, scopes=[_CLOUD_PLATFORM_SCOPE]
            )
        if key_json:
            return service_account.Credentials.from_service_account_info(
                json.loads(key_json), scopes=[_CLOUD_PLATFORM_SCOPE]
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Could not load Google Speech credentials: %s", exc)
        raise TranscriptionUnavailable(
            "Speech-to-text is not configured correctly on the server."
        ) from exc
    raise TranscriptionUnavailable(
        "Speech-to-text is not configured on the server."
    )


def _access_token(credentials):
    try:
        credentials.refresh(
            google_requests.Request(
                session=_TimeoutSession(_CREDENTIALS_TIMEOUT)
            )
        )
    except (GoogleAuthError, requests.RequestException) as exc:
        logger.warning("Could not fetch Google access token: %s", exc)
        raise TranscriptionError(
            "Speech service authentication failed. Please try again."
        ) from exc
    if not credentials.token:
        raise TranscriptionError(
            "Speech service authentication failed. Please try again."
        )
    return credentials.token


def _speech_error(response):
    try:
        detail = response.json().get("error", {}).get("message")
    except ValueError:
        detail = None
    if detail:
        return f"Speech service error: {detail}"
    return "The speech service returned an error. Please try again."
