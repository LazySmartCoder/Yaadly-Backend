from rest_framework import serializers


class DeviceTokenSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
    platform = serializers.ChoiceField(
        choices=["android", "ios", "web", "unknown"],
        default="unknown",
    )
