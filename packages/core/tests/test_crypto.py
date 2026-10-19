"""Envelope encryption: round trip, associated data, and data keys bound to their user."""

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

import boto3
import httpx2
import pytest
from botocore.stub import ANY, Stubber
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from copilot_core.config import DataSettings, gce_runtime
from copilot_core.crypto import (
    DecryptError,
    DevKeyProvider,
    GcpKmsKeyProvider,
    KmsKeyProvider,
    make_key_provider,
    open_sealed,
    seal,
)
from copilot_core.gcp import MetadataTokenSource


def dev_settings(**over: Any) -> DataSettings:
    base: dict[str, Any] = {
        "env": "dev",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
    }
    base.update(over)
    return DataSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


def test_seal_round_trip_and_associated_data() -> None:
    dek = os.urandom(32)
    row = str(uuid.uuid4())
    sealed = seal(dek, "questions", row, "Tell me about a time you failed.")
    assert b"failed" not in sealed
    assert open_sealed(dek, "questions", row, sealed) == "Tell me about a time you failed."
    # Same key, another row or table: the associated data differs, so it does not open.
    with pytest.raises(DecryptError):
        open_sealed(dek, "questions", str(uuid.uuid4()), sealed)
    with pytest.raises(DecryptError):
        open_sealed(dek, "answers", row, sealed)
    with pytest.raises(DecryptError):
        open_sealed(os.urandom(32), "questions", row, sealed)
    # Fresh nonce every time.
    assert seal(dek, "questions", row, "x") != seal(dek, "questions", row, "x")


async def test_dev_provider_binds_data_key_to_user() -> None:
    provider = DevKeyProvider.from_settings(dev_settings(), environ={})
    alice, bob = uuid.uuid4(), uuid.uuid4()
    dek, wrapped = await provider.generate_data_key(alice)
    assert dek not in wrapped
    assert await provider.decrypt_data_key(wrapped, alice) == dek
    with pytest.raises(DecryptError, match="another user"):
        await provider.decrypt_data_key(wrapped, bob)


def test_dev_provider_refuses_outside_dev() -> None:
    with pytest.raises(RuntimeError, match="COPILOT_ENV=dev"):
        DevKeyProvider.from_settings(dev_settings(env="prod"), environ={})
    with pytest.raises(RuntimeError, match="AWS"):
        DevKeyProvider.from_settings(dev_settings(), environ={"AWS_EXECUTION_ENV": "x"})
    with pytest.raises(RuntimeError, match="MASTER_KEY"):
        DevKeyProvider.from_settings(dev_settings(dev_master_key=None), environ={})


async def test_kms_provider_sends_user_encryption_context() -> None:
    client = boto3.client(
        "kms", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    uid = uuid.uuid4()
    context = {"user_id": str(uid)}
    with Stubber(client) as stub:
        stub.add_response(
            "generate_data_key",
            {"Plaintext": b"p" * 32, "CiphertextBlob": b"wrapped", "KeyId": "k"},
            {"KeyId": "alias/user-data", "KeySpec": "AES_256", "EncryptionContext": context},
        )
        stub.add_response(
            "decrypt",
            {"Plaintext": b"p" * 32, "KeyId": "k"},
            {
                "CiphertextBlob": b"wrapped",
                "KeyId": "alias/user-data",
                "EncryptionContext": context,
            },
        )
        stub.add_client_error(
            "decrypt",
            "InvalidCiphertextException",
            expected_params={"CiphertextBlob": ANY, "KeyId": ANY, "EncryptionContext": ANY},
        )
        provider = KmsKeyProvider("alias/user-data", "us-east-1", client=client)
        dek, wrapped = await provider.generate_data_key(uid)
        assert (dek, wrapped) == (b"p" * 32, b"wrapped")
        assert await provider.decrypt_data_key(wrapped, uid) == dek
        with pytest.raises(DecryptError):
            await provider.decrypt_data_key(wrapped, uuid.uuid4())
        stub.assert_no_pending_responses()


KMS_KEY = "projects/p/locations/us-central1/keyRings/copilot/cryptoKeys/user-data"


class FakeCloudKms:
    """Cloud KMS's encrypt and decrypt: AES-GCM under a hidden key with the request's
    additional authenticated data, so a wrong user id fails the way Cloud KMS does (400)."""

    def __init__(self) -> None:
        self.aead = AESGCM(os.urandom(32))
        self.requests: list[tuple[str, dict[str, Any], str]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, request.headers["Authorization"]))
        aad = base64.b64decode(body.get("additionalAuthenticatedData", ""))
        if request.url.path.endswith(":encrypt"):
            nonce = os.urandom(12)
            ct = nonce + self.aead.encrypt(nonce, base64.b64decode(body["plaintext"]), aad)
            return httpx2.Response(200, json={"ciphertext": base64.b64encode(ct).decode()})
        ct = base64.b64decode(body["ciphertext"])
        try:
            plain = self.aead.decrypt(ct[:12], ct[12:], aad)
        except InvalidTag:
            return httpx2.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
        return httpx2.Response(200, json={"plaintext": base64.b64encode(plain).decode()})


class FixedToken:
    async def token(self) -> str:
        return "ya29.test"


async def test_cloud_kms_provider_binds_data_key_to_user() -> None:
    kms = FakeCloudKms()
    provider = GcpKmsKeyProvider(
        KMS_KEY, FixedToken(), httpx2.AsyncClient(transport=httpx2.MockTransport(kms))
    )
    alice, bob = uuid.uuid4(), uuid.uuid4()
    dek, wrapped = await provider.generate_data_key(alice)
    assert len(dek) == 32
    assert dek not in wrapped
    assert wrapped.startswith(b"gkms1")
    assert await provider.decrypt_data_key(wrapped, alice) == dek
    with pytest.raises(DecryptError, match="HTTP 400"):
        await provider.decrypt_data_key(wrapped, bob)
    with pytest.raises(DecryptError, match="not a Cloud KMS"):
        await provider.decrypt_data_key(b"dev1" + wrapped[5:], alice)
    path, body, auth = kms.requests[0]
    assert path == f"/v1/{KMS_KEY}:encrypt"
    assert auth == "Bearer ya29.test"
    assert base64.b64decode(body["additionalAuthenticatedData"]) == str(alice).encode()


def test_cloud_kms_provider_needs_a_full_key_name() -> None:
    with pytest.raises(RuntimeError, match="cryptoKeys"):
        GcpKmsKeyProvider("user-data", FixedToken())
    with pytest.raises(RuntimeError, match="GCP_KMS_KEY"):
        make_key_provider(DataSettings(_env_file=None, key_provider="gcp_kms"))  # pyright: ignore[reportCallIssue]
    made = make_key_provider(
        DataSettings(_env_file=None, key_provider="gcp_kms", gcp_kms_key=KMS_KEY)  # pyright: ignore[reportCallIssue]
    )
    assert isinstance(made, GcpKmsKeyProvider)


async def test_metadata_tokens_are_cached_until_near_expiry() -> None:
    now = [0.0]
    calls: list[int] = []

    async def fetch() -> dict[str, Any]:
        calls.append(1)
        return {"access_token": f"t{len(calls)}", "expires_in": 3599}

    source = MetadataTokenSource(fetch, clock=lambda: now[0])
    assert await source.token() == "t1"
    now[0] = 3000.0
    assert await source.token() == "t1"
    now[0] = 3500.0  # within two minutes of expiry
    assert await source.token() == "t2"


def test_gce_marker_reads_the_dmi_product_name(tmp_path: Path) -> None:
    product = tmp_path / "product_name"
    assert gce_runtime(product) == []
    product.write_text("Google Compute Engine\n")
    assert gce_runtime(product) == ["GCE"]
    product.write_text("Standard PC (Q35 + ICH9, 2009)\n")
    assert gce_runtime(product) == []
