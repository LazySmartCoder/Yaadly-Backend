import logging

import requests

logger = logging.getLogger(__name__)

GOOGLE_PEOPLE_API_URL = "https://people.googleapis.com/v1/people/me"
GOOGLE_PEOPLE_PERSON_FIELDS = "phoneNumbers,birthdays,genders,addresses,emailAddresses"
GOOGLE_PEOPLE_TIMEOUT = 10

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_REVOKE_TIMEOUT = 10


def revoke_google_token(access_token):
    """Best-effort revoke of the user's Google OAuth grant for Yaadly.

    Hitting Google's revoke endpoint removes Yaadly from the user's "apps with
    access" list and invalidates every token Yaadly holds, so a future sign-in
    with the same Google account shows the full consent screen again and the
    account is treated as a fresh sign-up.

    This is strictly best-effort: a missing token, network error, or non-200
    response is logged and swallowed so account deletion always proceeds.
    Returns True when Google confirmed the revocation.
    """
    if not access_token:
        return False

    try:
        response = requests.post(
            GOOGLE_REVOKE_URL,
            params={"token": access_token},
            timeout=GOOGLE_REVOKE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Google token revocation request failed: %s", exc)
        return False

    if response.status_code != 200:
        logger.warning(
            "Google token revocation returned HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )
        return False
    return True


def fetch_google_profile(access_token, expected_google_id=None, expected_email=None):
    """Fetch optional, consented profile fields from the Google People API.

    Uses the OAuth access token returned by Google Sign-In for the read-only
    People API scopes. Returns a dict of normalized fields and is strictly
    best-effort: missing data, denied scopes, network errors, and API failures
    are logged and swallowed so Google sign-in never fails because optional
    profile enrichment is unavailable.

    When ``expected_google_id`` is provided, the fetched profile is discarded
    (empty dict returned) unless the People API response identifies the same
    Google account. The account is identified via the response's
    ``resourceName`` (``people/<account_id>``, where the id equals the ID
    token ``sub`` claim). ``expected_email`` is used as a fallback when the
    response has no ``resourceName`` (or as the primary check when no Google id
    is known), so a reused or mismatched access token can never enrich the
    wrong user.

    Returns:
        dict with any of: ``phone_number``, ``birthday``, ``gender``,
        ``addresses`` (list of formatted address strings). May be empty.
    """
    if not access_token:
        return {}

    try:
        response = requests.get(
            GOOGLE_PEOPLE_API_URL,
            params={"personFields": GOOGLE_PEOPLE_PERSON_FIELDS},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=GOOGLE_PEOPLE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Google People API request failed: %s", exc)
        return {}

    if response.status_code != 200:
        logger.warning(
            "Google People API rejected the token (HTTP %s): %s",
            response.status_code,
            response.text[:200],
        )
        return {}

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Google People API returned a non-JSON response.")
        return {}

    fields = _extract_profile_fields(payload)
    if not _owns_account(payload, expected_google_id, expected_email):
        logger.warning(
            "Google People API account mismatch for %s; skipping enrichment.",
            expected_google_id or expected_email or "(unknown)",
        )
        return {}
    return fields


def _owns_account(payload, expected_google_id=None, expected_email=None):
    """Whether the fetched profile belongs to the expected Google account.

    ``resourceName`` (``people/<account_id>``) is the authoritative check when
    the account id is known; it is always returned by people/me and does not
    depend on optional fields like emailAddresses being present. The email
    check is a fallback for payloads missing resourceName.
    """
    resource_id = _resource_id(payload)
    if expected_google_id is not None:
        if resource_id is not None:
            return resource_id == str(expected_google_id)
        if expected_email is not None:
            return _matches_email(payload, expected_email)
        return False
    if expected_email is not None:
        return _matches_email(payload, expected_email)
    return True


def _resource_id(payload):
    resource = payload.get("resourceName") or ""
    if resource.startswith("people/"):
        value = resource[len("people/"):].strip()
        if value:
            return value
    return None


def _matches_email(payload, expected_email):
    emails = {
        (entry.get("value") or "").strip().lower()
        for entry in payload.get("emailAddresses") or []
        if (entry.get("value") or "").strip()
    }
    return bool(emails) and expected_email.strip().lower() in emails


def _extract_profile_fields(payload):
    fields = {}

    for entry in payload.get("phoneNumbers") or []:
        value = (entry.get("value") or entry.get("canonicalForm") or "").strip()
        if value:
            fields["phone_number"] = value
            break

    for entry in payload.get("birthdays") or []:
        date = entry.get("date") or {}
        year, month, day = date.get("year"), date.get("month"), date.get("day")
        if month and day:
            if year:
                fields["birthday"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            else:
                fields["birthday"] = f"{int(month):02d}-{int(day):02d}"
            break

    for entry in payload.get("genders") or []:
        value = (entry.get("value") or "").strip().lower()
        if value:
            fields["gender"] = value
            break

    addresses = [
        entry["formattedValue"].strip()
        for entry in payload.get("addresses") or []
        if (entry.get("formattedValue") or "").strip()
    ]
    if addresses:
        fields["addresses"] = addresses

    return fields
