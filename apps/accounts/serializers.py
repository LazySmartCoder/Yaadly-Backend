from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import UserProfile


class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    bio = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    birthday = serializers.SerializerMethodField()
    gender = serializers.SerializerMethodField()
    addresses = serializers.SerializerMethodField()
    day_streak = serializers.SerializerMethodField()
    premium_user = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = [
            "id",
            "email",
            "name",
            "avatar_url",
            "bio",
            "phone_number",
            "birthday",
            "gender",
            "addresses",
            "day_streak",
            "premium_user",
        ]

    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def get_name(self, obj):
        profile = self._profile(obj)
        return profile.display_name if profile and profile.display_name else ""

    def get_avatar_url(self, obj):
        profile = self._profile(obj)
        return profile.avatar_url if profile and profile.avatar_url else ""

    def get_bio(self, obj):
        profile = self._profile(obj)
        return profile.bio if profile and profile.bio else ""

    def get_phone_number(self, obj):
        profile = self._profile(obj)
        return profile.phone_number if profile and profile.phone_number else ""

    def get_birthday(self, obj):
        profile = self._profile(obj)
        return profile.birthday if profile and profile.birthday else ""

    def get_gender(self, obj):
        profile = self._profile(obj)
        return profile.gender if profile and profile.gender else ""

    def get_addresses(self, obj):
        profile = self._profile(obj)
        return profile.addresses if profile and profile.addresses else []

    def get_day_streak(self, obj):
        profile = self._profile(obj)
        if not profile:
            return 0
        return profile.recompute_day_streak()

    def get_premium_user(self, obj):
        profile = self._profile(obj)
        return bool(profile and profile.premium_user)


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField(trim_whitespace=False)


class GoogleProfileSerializer(serializers.Serializer):
    access_token = serializers.CharField(trim_whitespace=False)


class DeleteAccountSerializer(serializers.Serializer):
    access_token = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False
    )
