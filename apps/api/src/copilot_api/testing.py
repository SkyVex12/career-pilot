"""Test support for the API (apps/api/tests): settings for the local stack and `Api`, the
client and checks the tests use."""

import base64
import os
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx2

from copilot_api.config import ApiSettings
from copilot_api.deps import Services
from copilot_core.auth import Principal, StaticVerifier
from copilot_core.jobs import MemoryJobQueue
from copilot_core.teststack import (
    S3_ENDPOINT,
    VALKEY_URL,
)


def api_settings(**over: Any) -> ApiSettings:
    base: dict[str, Any] = {
        "env": "test",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "valkey_url": VALKEY_URL,
        "max_document_bytes": 64 * 1024,
    }
    base.update(over)
    return ApiSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@dataclass
class Api:
    svc: Services
    client: httpx2.AsyncClient
    admin: asyncpg.Connection
    jobs: MemoryJobQueue
    verifier: StaticVerifier

    def user(self, name: str = "Alex") -> tuple[uuid.UUID, dict[str, str]]:
        """A new user's id and auth headers (their row is created on first request)."""
        uid = uuid.uuid4()
        token = f"token-{uid}"
        self.verifier.principals[token] = Principal(uid, name, None, email=f"{uid.hex}@example.com")
        return uid, {"Authorization": f"Bearer {token}"}

    def take(self, job_type: str) -> list[dict[str, Any]]:
        out = [m for m in self.jobs.sent if m["type"] == job_type]
        self.jobs.sent = [m for m in self.jobs.sent if m not in out]
        return out
