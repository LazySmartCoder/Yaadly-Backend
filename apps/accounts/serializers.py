from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import UserProfile


class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    birthday = serializers.SerializerMethodField()
    gender = serializers.SerializerMethodField()
    addresses = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = [
            "id",
            "email",
            "name",
            "avatar_url",
            "phone_number",
            "birthday",
            "gender",
            "addresses",
        ]

    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def get_name(self, obj):
        profile = self._profile(obj)
        return profile.display_name if profile and profile.display_name else ""

    def get_avatar_url(self, obj):
        profile = self._profile(obj)
        return profile.avatar_url if profile and profile.avatar_url else ""

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


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField(trim_whitespace=False)


class GoogleProfileSerializer(serializers.Serializer):
    access_token = serializers.CharField(trim_whitespace=False)


class DeleteAccountSerializer(serializers.Serializer):
    access_token = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False
    )
