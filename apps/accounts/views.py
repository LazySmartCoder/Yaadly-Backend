import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import UserProfile
from .serializers import (
    DeleteAccountSerializer,
    GoogleAuthSerializer,
    GoogleProfileSerializer,
    UserSerializer,
)
from .services import fetch_google_profile, revoke_google_token

logger = logging.getLogger(__name__)
User = get_user_model()

# google-auth's default transport has NO request timeout, so a slow or
# unreachable Google endpoint could hang the login request indefinitely. Enforce
# a hard timeout on every call it makes (certificate fetch, token-info check).
GOOGLE_TOKEN_VERIFY_TIMEOUT = 8


class _TimeoutSession(requests.Session):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, **kwargs)


_google_verify_request = google_requests.Request(
    session=_TimeoutSession(GOOGLE_TOKEN_VERIFY_TIMEOUT)
)


class GoogleLoginView(APIView):
    """Verify a Google ID token and return Yaadly JWT tokens.

    Never trusts client-provided user data; the only input is the ID token,
    verified server-side against Google using the configured audience. Optional
    People API enrichment is deliberately NOT part of this request so sign-in
    never waits on (or fails because of) the Google People API; it happens in
    GoogleProfileView after authentication.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = GoogleAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        try:
            info = id_token.verify_oauth2_token(
                validated["id_token"],
                _google_verify_request,
                audience=settings.GOOGLE_CLIENT_ID,
            )
        except requests.Timeout as exc:
            logger.warning("Google token verification timed out: %s", exc)
            return Response(
                {"detail": "Could not verify the Google sign-in. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except (GoogleAuthError, ValueError, requests.RequestException) as exc:
            logger.warning("Google token verification failed: %s", exc)
            return Response(
                {"detail": "Invalid Google ID token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        google_id = str(info.get("sub", ""))
        email = (info.get("email") or "").lower()
        name = info.get("name") or ""
        avatar = info.get("picture") or ""
        if not google_id or not email:
            return Response(
                {"detail": "Google token is missing required claims."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = self._get_or_create_user(google_id, email, name, avatar)
        if user is None:
            return Response(
                {"detail": "Could not create or find a matching account."},
                status=status.HTTP_409_CONFLICT,
            )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            }
        )

    @staticmethod
    def _get_or_create_user(google_id, email, name, avatar):
        with transaction.atomic():
            profile = UserProfile.objects.select_for_update().filter(
                google_id=google_id
            ).first()
            if profile:
                profile.display_name = name or profile.display_name
                if avatar:
                    profile.avatar_url = avatar
                profile.save(
                    update_fields=["display_name", "avatar_url", "updated_at"]
                )
                return profile.user

            existing = User.objects.filter(email__iexact=email).first()
            if existing:
                UserProfile.objects.update_or_create(
                    user=existing,
                    defaults={
                        "google_id": google_id,
                        "display_name": name,
                        "avatar_url": avatar,
                    },
                )
                return existing

            try:
                user = User.objects.create_user(username=email, email=email)
                UserProfile.objects.create(
                    user=user,
                    google_id=google_id,
                    display_name=name,
                    avatar_url=avatar,
                )
                return user
            except IntegrityError as exc:
                logger.warning("User creation raced for %s: %s", email, exc)
                profile = UserProfile.objects.filter(google_id=google_id).first()
                if profile:
                    return profile.user
                existing = User.objects.filter(email__iexact=email).first()
                if existing:
                    return existing
                raise


class GoogleProfileView(APIView):
    """Enrich the authenticated user's profile with consented Google People API
    data (phone number, birthday, gender, addresses).

    Runs after sign-in so optional profile fields can never block or fail
    authentication. Authenticates with the Yaadly JWT access token and verifies
    the supplied Google OAuth access token belongs to the same account before
    storing anything. Best-effort and idempotent: returns the (possibly
    enriched) user either way.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = GoogleProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        access_token = serializer.validated_data["access_token"]

        user = request.user
        profile, _ = UserProfile.objects.get_or_create(user=user)
        people = {}
        try:
            people = fetch_google_profile(
                access_token,
                expected_google_id=profile.google_id,
                expected_email=(user.email or "").lower(),
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must be best-effort
            logger.warning("Google People API enrichment failed: %s", exc)
        if people:
            fields = _people_fields(people)
            for key, value in fields.items():
                setattr(profile, key, value)
            profile.save(update_fields=["updated_at", *fields])

        return Response({"user": UserSerializer(user).data})


class DeleteAccountView(APIView):
    """Permanently delete the authenticated user's account.

    Revokes the user's Yaadly Google OAuth grant first (best-effort, using the
    Google access token the client supplies) so Google drops Yaadly from the
    user's authorized apps and re-asks for consent on a future sign-in, then
    deletes the account and all of its data (profile, journal entries, etc.)
    from the database. Revocation never blocks deletion: the account is removed
    even if Google is unreachable.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request):
        serializer = DeleteAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        access_token = serializer.validated_data.get("access_token") or ""

        if access_token:
            revoke_google_token(access_token)

        user = request.user
        with transaction.atomic():
            user.delete()
        return Response({"detail": "Account deleted."}, status=status.HTTP_200_OK)


def _people_fields(people):
    if not people:
        return {}
    fields = {}
    for key in ("phone_number", "birthday", "gender"):
        if people.get(key):
            fields[key] = people[key]
    if people.get("addresses"):
        fields["addresses"] = people["addresses"]
    return fields
