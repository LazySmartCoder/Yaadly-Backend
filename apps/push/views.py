import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DeviceToken
from .serializers import DeviceTokenSerializer

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
