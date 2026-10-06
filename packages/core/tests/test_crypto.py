"""Envelope encryption: round trip, associated data, and data keys bound to their user."""

import base64
import os
import uuid
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from copilot_core.config import DataSettings
from copilot_core.crypto import (
    DecryptError,
    DevKeyProvider,
    KmsKeyProvider,
    open_sealed,
    seal,
)


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
