import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_actions import send_remember_pushes
from .models import DeviceToken
from .serializers import DeviceTokenSerializer
from .services import configuration_diagnostics, is_configured

logger = logging.getLogger(__name__)


class RegisterDeviceTokenView(APIView):
    """Register (or re-register) the authenticated user's FCM device token.

    Idempotent upsert keyed on the token itself: signing in on a new device
    re-assigns the token to the current user, and re-opening the app simply
    refreshes the stored platform/active state.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = (serializer.validated_data["token"] or "").strip()
        if not token:
            return Response(
                {"detail": "A device token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        platform = serializer.validated_data["platform"]

        existing = DeviceToken.objects.filter(token=token).first()
        if existing is not None:
            if existing.user_id != request.user.id:
                existing.user = request.user
                existing.is_active = True
            existing.platform = platform
            existing.save(update_fields=["user", "platform", "is_active", "updated_at"])
        else:
            DeviceToken.objects.create(
                user=request.user,
                token=token,
                platform=platform,
            )

        return Response(
            {"detail": "Device token registered."},
            status=status.HTTP_200_OK,
        )


class DeactivateDeviceTokenView(APIView):
    """Deactivate the user's device token(s), e.g. on logout.

    With a ``token`` in the body only that token is deactivated (if the user
    owns it); without one, every token of the current user is deactivated. This
    stops the daily pushes for logged-out users without deleting history.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token_value = (request.data.get("token") or "").strip()
        qs = DeviceToken.objects.filter(user=request.user, is_active=True)
        if token_value:
            qs = qs.filter(token=token_value)
        count = qs.update(is_active=False, updated_at=timezone.now())
        logger.info("Deactivated %s device token(s) for user %s", count, request.user.id)
        return Response(
            {"detail": f"{count} device token(s) deactivated."},
            status=status.HTTP_200_OK,
        )


@staff_member_required
@require_POST
def admin_shoot(request):
    """Admin "Shoot" action: push every user a "Remember?" notification with a
    photo from a random one of their journal entries, then bounce back to the
    admin index with a summary. POST-only so it can never be triggered by a
    stray GET; requires an authenticated staff account."""
    if not is_configured():
        messages.error(
            request,
            "FCM is not configured: "
            f"{configuration_diagnostics()}. No pushes were sent.",
        )
        return HttpResponseRedirect(reverse("admin:index"))

    summary = send_remember_pushes()
    messages.success(
        request,
        "Shoot complete: %(sent)s push(es) sent to %(targeted)s user(s); "
        "%(failed)s failed, %(skipped)s user(s) had no photo entry."
        % {
            "sent": summary["tokens_sent"],
            "targeted": summary["users_targeted"],
            "failed": summary["tokens_failed"],
            "skipped": summary["users_skipped"],
        },
    )
    return HttpResponseRedirect(reverse("admin:index"))
