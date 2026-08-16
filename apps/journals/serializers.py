from urllib.parse import urlparse

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework import serializers

from .models import Gallery, JournalEntry


def _fresh_url(url):
    """Regenerates the object-storage URL for a Gallery row so every photo
    link is served as a current pre-signed URL, which works even when the
    bucket isn't publicly readable."""
    if not url:
        return ""
    key = _storage_key(url)
    if settings.USE_S3_STORAGE and settings.AWS_SIGNING_ACCESS_KEY_ID:
        return _presigned_url(key)
    try:
        return default_storage.url(key)
    except Exception:
        return url


def _presigned_url(key):
    """Mints a fresh pre-signed GET URL with the dedicated read-only
    credentials, so photos can be viewed without making the bucket public."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        region_name=settings.AWS_S3_REGION_NAME,
        aws_access_key_id=settings.AWS_SIGNING_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SIGNING_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=settings.AWS_SIGNED_URL_EXPIRE,
    )


def _storage_key(url):
    """Derives the storage name a stored URL points at, handling S3-style
    public URLs (which include the bucket name) and the local MEDIA_URL
    prefix."""
    if not url:
        return ""
    key = urlparse(url).path.lstrip("/")
    media = settings.MEDIA_URL.strip("/")
    if media and key.startswith(media + "/"):
        key = key[len(media) + 1:]
    bucket = getattr(default_storage, "bucket_name", None)
    if isinstance(bucket, str) and key.startswith(bucket + "/"):
        key = key[len(bucket) + 1:]
    return key


class JournalEntrySerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=255, required=False, allow_blank=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id",
            "date",
            "title",
            "content",
            "mood",
            "type",
            "aid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class GallerySerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = Gallery
        fields = ["id", "url", "created_at"]
        read_only_fields = ["id", "url", "created_at"]

    def get_url(self, obj):
        return _fresh_url(obj.url)
