"""Per-user envelope encryption (ARCHITECTURE.md, "Security and privacy" and "Conventions").

Each user has a data key (DEK) in `app.user_keys`, stored encrypted by the KMS key with the
encryption context `{"user_id": <uid>}`, so it decrypts only for that user. `_enc` columns hold
AES-256-GCM ciphertext made with the DEK; the associated data is the table name plus the row
id, so ciphertext copied to another row (or table) does not decrypt.

Ciphertext layout: one format byte (1), a 12-byte random nonce, then the GCM output (ciphertext
and 16-byte tag).

Key providers:
- `KmsKeyProvider`: AWS KMS GenerateDataKey and Decrypt with the encryption context.
- `GcpKmsKeyProvider`: Cloud KMS on GCP. Cloud KMS has no GenerateDataKey and no encryption
  context: the DEK is made here and wrapped with Encrypt, with the user id as additional
  authenticated data, so Decrypt opens it only when given the same user id (the encryption
  context's guarantee).
- `DevKeyProvider`: local stand-in. A 32-byte master key from settings wraps each DEK with
  AES-256-GCM, with the user id as associated data: the same guarantee as the KMS encryption
  context (a wrapped key opens only for its user), with no container to run. It refuses to
  start outside COPILOT_ENV=dev and on cloud compute.
"""

import asyncio
import base64
import os
import time
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import httpx2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from copilot_core.auth import refuse_outside_dev
from copilot_core.aws import boto3_client
from copilot_core.config import DataSettings
from copilot_core.db import Conn
from copilot_core.gcp import MetadataTokenSource, TokenSource

FORMAT_V1 = b"\x01"
NONCE_BYTES = 12
DEV_WRAP_PREFIX = b"dev1"
GCP_WRAP_PREFIX = b"gkms1"
CLOUD_KMS_URL = "https://cloudkms.googleapis.com/v1"


class DecryptError(Exception):
    pass


class KeyProvider(Protocol):
    async def generate_data_key(self, user_id: UUID) -> tuple[bytes, bytes]:
        """A new 32-byte data key for the user: (plaintext, wrapped for storage)."""
        ...

    async def decrypt_data_key(self, wrapped: bytes, user_id: UUID) -> bytes:
        """Unwrap a data key; raises DecryptError if it is not this user's."""
        ...


def _context(user_id: UUID) -> dict[str, str]:
    return {"user_id": str(user_id)}


class KmsKeyProvider:
    def __init__(self, key_id: str, region: str, client: Any = None) -> None:
        self.key_id = key_id
        self.client: Any = client or boto3_client("kms", region_name=region)

    async def generate_data_key(self, user_id: UUID) -> tuple[bytes, bytes]:
        response = await asyncio.to_thread(
            self.client.generate_data_key,
            KeyId=self.key_id,
            KeySpec="AES_256",
            EncryptionContext=_context(user_id),
        )
        return bytes(response["Plaintext"]), bytes(response["CiphertextBlob"])

    async def decrypt_data_key(self, wrapped: bytes, user_id: UUID) -> bytes:
        try:
            response = await asyncio.to_thread(
                self.client.decrypt,
                CiphertextBlob=wrapped,
                KeyId=self.key_id,
                EncryptionContext=_context(user_id),
            )
        except Exception as e:  # InvalidCiphertextException when the context differs
            raise DecryptError(f"KMS refused the data key: {type(e).__name__}") from e
        return bytes(response["Plaintext"])


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class GcpKmsKeyProvider:
    """Cloud KMS through its REST API, with the VM service account's token."""

    def __init__(
        self,
        key_name: str,
        tokens: TokenSource | None = None,
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        if not key_name.startswith("projects/") or "/cryptoKeys/" not in key_name:
            raise RuntimeError(
                "COPILOT_GCP_KMS_KEY must be projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>"
            )
        self.key_name = key_name
        self.tokens: TokenSource = tokens or MetadataTokenSource()
        self.http = http or httpx2.AsyncClient(timeout=10.0)

    async def _call(self, verb: str, body: dict[str, str]) -> dict[str, Any]:
        token = await self.tokens.token()
        response = await self.http.post(
            f"{CLOUD_KMS_URL}/{self.key_name}:{verb}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            # The body names the reason (wrong AAD reads as INVALID_ARGUMENT); never the key.
            raise DecryptError(f"Cloud KMS {verb} failed: HTTP {response.status_code}")
        data: dict[str, Any] = response.json()
        return data

    async def generate_data_key(self, user_id: UUID) -> tuple[bytes, bytes]:
        dek = os.urandom(32)
        data = await self._call(
            "encrypt",
            {"plaintext": _b64(dek), "additionalAuthenticatedData": _b64(str(user_id).encode())},
        )
        return dek, GCP_WRAP_PREFIX + base64.b64decode(data["ciphertext"])

    async def decrypt_data_key(self, wrapped: bytes, user_id: UUID) -> bytes:
        if not wrapped.startswith(GCP_WRAP_PREFIX):
            raise DecryptError("not a Cloud KMS-wrapped data key")
        data = await self._call(
            "decrypt",
            {
                "ciphertext": _b64(wrapped[len(GCP_WRAP_PREFIX) :]),
                "additionalAuthenticatedData": _b64(str(user_id).encode()),
            },
        )
        dek = base64.b64decode(data["plaintext"])
        if len(dek) != 32:
            raise DecryptError("Cloud KMS returned a data key of the wrong size")
        return dek


class DevKeyProvider:
    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise RuntimeError("COPILOT_DEV_MASTER_KEY must be base64 of 32 bytes")
        self._aead = AESGCM(master_key)

    @classmethod
    def from_settings(
        cls, settings: DataSettings, environ: Mapping[str, str] | None = None
    ) -> "DevKeyProvider":
        if settings.env != "test":
            refuse_outside_dev(
                settings, "the dev key provider", os.environ if environ is None else environ
            )
        if settings.dev_master_key is None:
            raise RuntimeError(
                "COPILOT_KEY_PROVIDER=dev needs COPILOT_DEV_MASTER_KEY (base64 of 32 bytes); "
                'make one with: uv run python -c "import os,base64;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        return cls(base64.b64decode(settings.dev_master_key.get_secret_value()))

    async def generate_data_key(self, user_id: UUID) -> tuple[bytes, bytes]:
        dek = os.urandom(32)
        nonce = os.urandom(NONCE_BYTES)
        wrapped = DEV_WRAP_PREFIX + nonce + self._aead.encrypt(nonce, dek, user_id.bytes)
        return dek, wrapped

    async def decrypt_data_key(self, wrapped: bytes, user_id: UUID) -> bytes:
        if not wrapped.startswith(DEV_WRAP_PREFIX):
            raise DecryptError("not a dev-wrapped data key")
        body = wrapped[len(DEV_WRAP_PREFIX) :]
        try:
            return self._aead.decrypt(body[:NONCE_BYTES], body[NONCE_BYTES:], user_id.bytes)
        except InvalidTag as e:
            raise DecryptError("data key belongs to another user") from e


def make_key_provider(settings: DataSettings) -> KeyProvider:
    if settings.key_provider == "dev":
        return DevKeyProvider.from_settings(settings)
    if settings.key_provider == "gcp_kms":
        if not settings.gcp_kms_key:
            raise RuntimeError("COPILOT_KEY_PROVIDER=gcp_kms needs COPILOT_GCP_KMS_KEY")
        return GcpKmsKeyProvider(settings.gcp_kms_key)
    if not settings.kms_key_id:
        raise RuntimeError("COPILOT_KEY_PROVIDER=kms needs COPILOT_KMS_KEY_ID")
    return KmsKeyProvider(settings.kms_key_id, settings.aws_region)


def associated_data(table: str, row_id: str) -> bytes:
    return f"app.{table}:{row_id}".encode()


def seal(dek: bytes, table: str, row_id: str, plaintext: str) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), associated_data(table, row_id))
    return FORMAT_V1 + nonce + ct


def open_sealed(dek: bytes, table: str, row_id: str, sealed: bytes) -> str:
    if not sealed.startswith(FORMAT_V1) or len(sealed) < 1 + NONCE_BYTES + 16:
        raise DecryptError("unknown ciphertext format")
    nonce, ct = sealed[1 : 1 + NONCE_BYTES], sealed[1 + NONCE_BYTES :]
    try:
        plain = AESGCM(dek).decrypt(nonce, ct, associated_data(table, row_id))
    except InvalidTag as e:
        raise DecryptError(f"ciphertext does not open for app.{table} row {row_id}") from e
    return plain.decode("utf-8")


class UserCrypto:
    """Data keys per user, with a short in-memory cache of unwrapped keys.

    Every method takes a connection whose transaction already set `app.user_id` (or that
    bypasses row-level security, for system jobs), since `user_keys` is a user table.
    """

    def __init__(self, provider: KeyProvider, cache_ttl_s: float = 300.0) -> None:
        self.provider = provider
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[tuple[UUID, int], tuple[bytes, float]] = {}

    def forget(self, user_id: UUID) -> None:
        for key in [k for k in self._cache if k[0] == user_id]:
            del self._cache[key]

    async def current_key(self, conn: Conn, user_id: UUID) -> tuple[int, bytes]:
        """The user's newest live key, created on first use."""
        row = await conn.fetchrow(
            "select version, encrypted_dek from app.user_keys"
            " where user_id = $1 and destroyed_at is null order by version desc limit 1",
            user_id,
        )
        if row is None:
            dek, wrapped = await self.provider.generate_data_key(user_id)
            # Two jobs may race to create the first key; the loser reads the winner's.
            inserted = await conn.fetchval(
                "insert into app.user_keys (user_id, version, encrypted_dek) values ($1, 1, $2)"
                " on conflict do nothing returning version",
                user_id,
                wrapped,
            )
            if inserted is not None:
                self._cache[(user_id, 1)] = (dek, time.monotonic())
                return 1, dek
            return await self.current_key(conn, user_id)
        version = int(row["version"])
        return version, await self._unwrap(user_id, version, bytes(row["encrypted_dek"]))

    async def key(self, conn: Conn, user_id: UUID, version: int) -> bytes:
        cached = self._cache.get((user_id, version))
        if cached is not None and time.monotonic() - cached[1] < self.cache_ttl_s:
            return cached[0]
        wrapped = await conn.fetchval(
            "select encrypted_dek from app.user_keys"
            " where user_id = $1 and version = $2 and destroyed_at is null",
            user_id,
            version,
        )
        if wrapped is None:
            raise DecryptError(f"no live data key {version} for this user")
        return await self._unwrap(user_id, version, bytes(wrapped))

    async def _unwrap(self, user_id: UUID, version: int, wrapped: bytes) -> bytes:
        cached = self._cache.get((user_id, version))
        if cached is not None and time.monotonic() - cached[1] < self.cache_ttl_s:
            return cached[0]
        dek = await self.provider.decrypt_data_key(wrapped, user_id)
        self._cache[(user_id, version)] = (dek, time.monotonic())
        return dek

    async def encrypt(
        self, conn: Conn, user_id: UUID, table: str, row_id: str, plaintext: str
    ) -> tuple[bytes, int]:
        version, dek = await self.current_key(conn, user_id)
        return seal(dek, table, row_id, plaintext), version

    async def decrypt(
        self,
        conn: Conn,
        user_id: UUID,
        table: str,
        row_id: str,
        sealed: bytes,
        version: int,
    ) -> str:
        return open_sealed(await self.key(conn, user_id, version), table, row_id, sealed)


def segment_row_id(session_id: UUID | str, channel: str, seg_id: int) -> str:
    """transcript_segments has a composite key; this is its row id for associated data."""
    return f"{session_id}:{channel}:{seg_id}"
