from django.conf import settings
from django.db import models


class DeviceToken(models.Model):
    """An FCM registration token for one of the user's devices.

    A user can have several devices; a device can only ever be owned by the
    user who last registered it (tokens are reassigned on sign-in). Tokens that
    FCM reports as unregistered are deactivated instead of deleted so they can
    be inspected (and the user can re-register by simply re-opening the app).
    """

    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"
        WEB = "web", "Web"
        UNKNOWN = "unknown", "Unknown"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="device_tokens",
    )
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(
        max_length=16,
        choices=Platform.choices,
        default=Platform.UNKNOWN,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self):
        return f"{self.token[:24]}... ({self.user_id})"
