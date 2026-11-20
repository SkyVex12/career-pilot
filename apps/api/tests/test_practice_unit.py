"""Practice API security and real provider wire adapters with deterministic transports."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from copilot_api.routes import practice
from copilot_core.auth import Principal


async def test_load_uses_account_key_and_explicit_profile_predicate():
    account, profile = Principal(uuid4(), "Account", None), Principal(uuid4(), "Profile", None)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"state_enc": b"sealed", "key_version": 2})
    svc = MagicMock()
    svc.crypto.decrypt = AsyncMock(return_value='{"status":"paused"}')
    sid = uuid4()
    _, state = await practice.load(conn, svc, account, profile, sid)
    assert state["status"] == "paused"
    assert "profile_id=$2" in conn.fetchrow.await_args.args[0]
    assert conn.fetchrow.await_args.args[1:] == (sid, profile.user_id)
    assert svc.crypto.decrypt.await_args is not None
    assert svc.crypto.decrypt.await_args.args[1] == account.user_id
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException) as missing:
        await practice.load(conn, svc, account, profile, sid)
    assert missing.value.status_code == 404
    assert svc.crypto.decrypt.await_count == 1


async def test_json_audio_wrapper_preserves_only_uploaded_microphone_bytes(monkeypatch):
    mock = AsyncMock(return_value=b"Candidate answer")
    monkeypatch.setattr(practice, "media", mock)
    body = practice.AudioIn(
        request_id=uuid4(),
        content_type="audio/webm;codecs=opus",
        audio_base64=base64.b64encode(b"microphone bytes").decode(),
    )
    principal = Principal(uuid4(), "Account", None)
    response = await practice.transcribe_json(uuid4(), body, MagicMock(), principal, principal)
    assert response == {"text": "Candidate answer", "silence": False, "audio_retained": False}
    assert mock.await_args is not None
    assert mock.await_args.args[-2:] == (b"microphone bytes", "audio/webm")
    assert mock.await_args.args[2] == "transcription"


async def test_json_audio_rejects_invalid_encoding_before_provider(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(practice, "media", mock)
    body = practice.AudioIn(request_id=uuid4(), content_type="audio/wav", audio_base64="!!!!")
    p = Principal(uuid4(), "Account", None)
    with pytest.raises(HTTPException) as invalid:
        await practice.transcribe_json(uuid4(), body, MagicMock(), p, p)
    assert invalid.value.status_code == 422
    mock.assert_not_awaited()


async def test_voice_adapter_uses_real_deepgram_contract_without_cross_channel_transcription(
    monkeypatch,
):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/speak"):
            return httpx2.Response(200, content=b"MP3 audio")
        return httpx2.Response(
            200, json={"results": {"channels": [{"alternatives": [{"transcript": "My example"}]}]}}
        )

    original = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kw: original(transport=httpx2.MockTransport(handler), **kw)
    )
    svc = MagicMock()
    svc.settings.deepgram_api_key = SecretStr("test-key")
    svc.settings.deepgram_model = "nova-3"
    svc.settings.verified_call_languages = ("en",)
    svc.settings.practice_tts_models = {"en": "aura-2-thalia-en"}
    assert (
        await practice.voice_request(svc, "speech", b"", "", "en", "Describe a project")
        == b"MP3 audio"
    )
    assert (
        await practice.voice_request(
            svc,
            "transcription",
            b"MIC ONLY",
            "audio/webm",
            "en",
            "Never transcribe this interviewer",
        )
        == b"My example"
    )
    assert json.loads(seen[0].content) == {"text": "Describe a project"}
    assert seen[1].content == b"MIC ONLY"
    assert seen[1].url.params["language"] == "en"
    assert seen[1].url.params["mip_opt_out"] == "true"


async def test_voice_unconfigured_is_honest_text_fallback():
    svc = MagicMock()
    svc.settings.deepgram_api_key = None
    with pytest.raises(HTTPException) as unavailable:
        await practice.voice_request(svc, "speech", b"", "", "en", "question")
    assert unavailable.value.status_code == 503
    assert "text mode" in unavailable.value.detail


def test_unsupported_audio_type_is_not_sent_to_provider():
    with pytest.raises(HTTPException) as invalid:
        practice.audio_type("text/html")
    assert invalid.value.status_code == 415
