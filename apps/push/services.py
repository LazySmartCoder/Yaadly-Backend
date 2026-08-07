"""Firebase Cloud Messaging sender (HTTP v1 API).

Sends notifications to the user's registered device tokens using FCM's HTTP v1
endpoint. Credentials come from a Firebase service-account JSON file (or inline
JSON via env), and OAuth2 access tokens are minted with google-auth (already a
project dependency). Strictly best-effort: a failing send is logged and never
raises into the caller unless the caller asked for it via [FcmSendError].
"""

import json
import logging
import os

import requests
from django.conf import settings
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_API_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
FCM_TIMEOUT = 10


class FcmError(Exception):
    """Base error for FCM sends."""


class DeviceNotRegistered(FcmError):
    """The token no longer maps to a device (uninstalled/revoked).

    Callers should deactivate the DeviceToken row so it is not retried.
    """


class FcmNotConfigured(FcmError):
    """No service-account credentials are configured (see FCM_* settings)."""


_credentials = None


def _load_credentials():
    """Load the Firebase service-account credentials once and cache them."""
    global _credentials
    if _credentials is not None:
        return _credentials

    path = getattr(settings, "FCM_SERVICE_ACCOUNT_PATH", "") or ""
    raw = getattr(settings, "FCM_SERVICE_ACCOUNT_JSON", "") or ""
    try:
        if path and os.path.exists(path):
            _credentials = service_account.Credentials.from_service_account_file(
                path,
                scopes=[FCM_SCOPE],
            )
        elif raw:
            info = json.loads(raw)
            _credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=[FCM_SCOPE],
            )
        else:
            return None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Failed to load FCM service-account credentials: %s", exc)
        return None
    return _credentials


def project_id():
    """The GCP project to route FCM requests to."""
    configured = getattr(settings, "FCM_PROJECT_ID", "") or ""
    creds = _load_credentials()
    return configured or getattr(creds, "project_id", "") or ""


def is_configured():
    """Whether FCM is usable right now (credentials + project id present)."""
    return bool(_load_credentials() and project_id())


def _access_token():
    creds = _load_credentials()
    if creds is None:
        raise FcmNotConfigured("FCM service-account credentials are not configured.")
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return creds.token


def send_message(token, title, body, data=None):
    """Send a single notification message to one device token.

    ``data`` (optional dict) is forwarded verbatim to the app as FCM data
    payload. Raises [DeviceNotRegistered] when FCM no longer recognizes the
    token, and [FcmError] (or [FcmNotConfigured]) for other failures.
    """
    token = (token or "").strip()
    title = (title or "").strip()
    body = (body or "").strip()
    if not token:
        raise DeviceNotRegistered("Cannot send to an empty device token.")
    if not title and not body:
        raise FcmError("A notification needs at least a title or a body.")

    project = project_id()
    if not project:
        raise FcmNotConfigured("FCM project id is not configured.")

    access = _access_token()
    payload = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {str(k): str(v) for k, v in (data or {}).items()},
            "android": {"priority": "HIGH"},
        }
    }

    try:
        response = requests.post(
            FCM_API_URL.format(project=project),
            headers={
                "Content-Type": "application/json; UTF-8",
                "Authorization": f"Bearer {access}",
            },
            json=payload,
            timeout=FCM_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("FCM send request failed: %s", exc)
        raise FcmError(str(exc)) from exc

    if response.status_code == 404:
        logger.info("FCM reports device token unregistered: %s...", token[:24])
        raise DeviceNotRegistered(response.text)
    if response.status_code >= 400:
        # Tokens that are invalid/expired arrive as 400 UNREGISTERED too.
        text = response.text
        if '"UNREGISTERED"' in text or '"INVALID_ARGUMENT"' in text:
            logger.info("FCM rejected unregistered/invalid token: %s...", token[:24])
            raise DeviceNotRegistered(text)
        logger.warning(
            "FCM send failed (HTTP %s): %s",
            response.status_code,
            text[:300],
        )
        raise FcmError(text)

    logger.info(
        "FCM message sent to %s... (HTTP %s)",
        token[:24],
        response.status_code,
    )
    return response.json() if response.content else {}
