"""S3 for uploaded documents and the user's own call audio (ARCHITECTURE.md, "Data and
storage").

Keys live under `uploads/{user_id}/`, the prefix the API's presigned uploads are limited to
and the one `user.delete` removes. Locally the store is SeaweedFS; in AWS the bucket's default
encryption is SSE-KMS (Terraform, build step 5); on GCP it is Cloud Storage through its
S3-compatible XML API with an HMAC key, presigned uploads included.
"""

import asyncio
from typing import Any, Protocol
from uuid import UUID

from copilot_core.aws import boto3_client
from copilot_core.config import DataSettings

EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mpeg": ".mp3",
}


def user_prefix(user_id: UUID) -> str:
    return f"uploads/{user_id}/"


def document_key(user_id: UUID, document_id: UUID, content_type: str) -> str:
    return f"{user_prefix(user_id)}documents/{document_id}{EXTENSIONS.get(content_type, '')}"


def employer_prefix(user_id: UUID, employer_id: UUID) -> str:
    """Everything stored for one company: its documents and work log attachments, sealed
    with the user's data key. "Delete all data for this company" removes this prefix."""
    return f"{user_prefix(user_id)}employers/{employer_id}/"


def company_document_key(user_id: UUID, employer_id: UUID, document_id: UUID) -> str:
    return f"{employer_prefix(user_id, employer_id)}{document_id}.sealed"


def me_audio_key(user_id: UUID, session_id: UUID) -> str:
    return f"{user_prefix(user_id)}me-audio/{session_id}"


class ObjectStore(Protocol):
    async def presign_put(
        self, key: str, content_type: str, expires_s: int = 900
    ) -> dict[str, Any]: ...

    async def head(self, key: str) -> dict[str, Any] | None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, body: bytes, content_type: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def ensure_bucket(self) -> None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


GCS_ENDPOINT = "storage.googleapis.com"


def is_gcs(endpoint: str | None) -> bool:
    """Cloud Storage's S3-compatible XML API (Stage 0 on GCP, with HMAC keys)."""
    return bool(endpoint) and GCS_ENDPOINT in str(endpoint)


def s3_client(settings: DataSettings) -> Any:
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": settings.s3_region or settings.aws_region,
        # Checksums only where S3 requires them: boto3's default sends CRC32 trailers in
        # aws-chunked bodies, which Cloud Storage (and older S3 stand-ins) refuse.
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    }
    if settings.s3_endpoint:
        kwargs["endpoint_url"] = settings.s3_endpoint
    if settings.s3_access_key and settings.s3_secret_key:
        kwargs["aws_access_key_id"] = settings.s3_access_key.get_secret_value()
        kwargs["aws_secret_access_key"] = settings.s3_secret_key.get_secret_value()
    return boto3_client("s3", **kwargs)


class S3Store:
    def __init__(self, settings: DataSettings, client: Any = None) -> None:
        self.bucket = settings.s3_bucket
        # Cloud Storage has no multi-object delete (POST ?delete); objects go one by one.
        self.batch_delete = not is_gcs(settings.s3_endpoint)
        self.client: Any = client or s3_client(settings)

    async def ensure_bucket(self) -> None:
        """Local stack only: create the bucket if it is missing (Terraform owns it in the
        cloud)."""
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except Exception as e:
            if not _not_found(e):
                raise
            await asyncio.to_thread(self.client.create_bucket, Bucket=self.bucket)

    async def presign_put(
        self, key: str, content_type: str, expires_s: int = 900
    ) -> dict[str, Any]:
        url = await asyncio.to_thread(
            self.client.generate_presigned_url,
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_s,
        )
        return {
            "method": "PUT",
            "url": url,
            "headers": {"Content-Type": content_type},
            "expires_in": expires_s,
        }

    async def head(self, key: str) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except Exception as e:
            if _not_found(e):
                return None
            raise

    async def get(self, key: str) -> bytes | None:
        try:
            response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        except Exception as e:
            if _not_found(e):
                return None
            raise
        return await asyncio.to_thread(response["Body"].read)

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def delete(self, key: str) -> None:
        # S3 answers a delete of a missing key with 204; Cloud Storage answers 404. Either way
        # the object is gone, which is all callers need.
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
        except Exception as e:
            if not _not_found(e):
                raise

    async def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = await asyncio.to_thread(self.client.list_objects_v2, **kwargs)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return keys
            token = page.get("NextContinuationToken")

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list_keys(prefix)
        if not self.batch_delete:
            for key in keys:
                await self.delete(key)
            return len(keys)
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i : i + 1000]]
            await asyncio.to_thread(
                self.client.delete_objects,
                Bucket=self.bucket,
                Delete={"Objects": batch, "Quiet": True},
            )
        return len(keys)


def _not_found(e: Exception) -> bool:
    response: Any = getattr(e, "response", None)
    try:
        code: Any = response["Error"]["Code"]
    except (TypeError, KeyError):
        return False
    return str(code) in ("404", "NoSuchKey", "NotFound", "NoSuchBucket")
