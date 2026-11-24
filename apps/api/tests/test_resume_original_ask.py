from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from copilot_api.routes import ask
from copilot_core import embed
from copilot_core.ask import Scope
from copilot_core.auth import Principal


def fixture() -> tuple[Any, Any, dict, Principal, Principal, list]:
    user, account = Principal(uuid4(), "Profile", None), Principal(uuid4(), "Account", None)
    row = {
        "id": uuid4(),
        "label": "Uploaded resume",
        "updated_at": "2026-11-20",
        "text_enc": None,
        "original_text_enc": b"sealed",
        "original_text_key_version": 7,
        "original_content_sha256": "current",
        "content_sha256": "current",
        "current_revision_id": uuid4(),
    }
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=row))

    async def fetch(sql, *args):
        return [row] if "select * from app.resume_versions" in sql else []

    conn.fetch = fetch

    @asynccontextmanager
    async def transaction():
        yield

    conn.transaction = transaction
    scopes = []

    @asynccontextmanager
    async def tx(principal):
        scopes.append(principal.user_id)
        yield conn

    svc = SimpleNamespace(
        tx=tx,
        crypto=SimpleNamespace(decrypt=AsyncMock(return_value="Original file text")),
        accept=AsyncMock(),
    )
    return svc, conn, row, user, account, scopes


async def test_file_original_resolution_rechecks_profile_row_and_content_hash():
    svc, conn, row, user, account, scopes = fixture()
    result = await ask.resolve_source("resume", str(row["id"]), svc, user, account)
    assert result["content"] == "Original file text"
    assert scopes == [user.user_id]
    assert svc.crypto.decrypt.await_args.args[2] == "resume_versions:original"
    row["content_sha256"] = "new file"
    with pytest.raises(HTTPException):
        await ask.resolve_source("resume", str(row["id"]), svc, user, account)
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException):
        await ask.resolve_source("resume", str(row["id"]), svc, user, account)


async def test_legacy_file_queues_worker_backfill_without_parsing_or_leaking_stale_text():
    svc, conn, row, user, account, _ = fixture()
    row["original_content_sha256"] = None
    assert (
        await ask.retrieve(conn, svc, user, Scope(), "resume", account.user_id, rank_results=False)
        == []
    )
    svc.crypto.decrypt.assert_not_awaited()
    job = svc.accept.await_args.args[1]
    assert job["type"] == "resume.original"
    assert job["user_id"] == str(user.user_id)
    row["original_content_sha256"] = "current"
    result = await ask.retrieve(
        conn, svc, user, Scope(), "resume", account.user_id, rank_results=False
    )
    assert result[0]["content"] == "Original file text"


async def test_per_request_embedder_closes_sync_and_async_clients(monkeypatch):
    for close in (MagicMock(), AsyncMock()):
        embedding = SimpleNamespace(
            client=SimpleNamespace(close=close), embed=AsyncMock(return_value=[[1.0], [1.0]])
        )
        monkeypatch.setattr(
            embed, "make_embedder", lambda *a, _embedding=embedding, **kw: _embedding
        )
        svc: Any = SimpleNamespace(settings=SimpleNamespace(ask_semantic_min_similarity=0.55))
        assert await ask.rank_sources(
            svc, "different language", [ask.source("resume", "r", "", "original", "today")]
        )
        close.assert_called_once()
