"""Language coverage, source relevance and capability failure behavior without paid calls."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2
import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from copilot_api.routes import ask as routes
from copilot_api.routes import practice, profiles
from copilot_api.testing import api_settings
from copilot_core import ask, embed, personal_ai
from copilot_core.auth import Principal
from copilot_core.config import CoreSettings
from copilot_core.languages import CALL_LANGUAGES, recognition_supported

CORPUS = json.loads(
    (Path(__file__).resolve().parents[3] / "tests/fixtures/call_languages.json").read_text("utf-8")
)


def core_settings() -> CoreSettings:
    return CoreSettings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_default_catalog_and_legacy_operator_allowlist(monkeypatch):
    monkeypatch.delenv("COPILOT_CALL_LANGUAGES", raising=False)
    monkeypatch.delenv("COPILOT_VERIFIED_CALL_LANGUAGES", raising=False)
    settings = core_settings()
    assert len(settings.verified_call_languages) == 24
    monkeypatch.setenv("COPILOT_VERIFIED_CALL_LANGUAGES", "en,vi")
    assert core_settings().verified_call_languages == ("en", "vi")
    monkeypatch.setenv("COPILOT_CALL_LANGUAGES", "en,ja,ja,ar")
    assert core_settings().verified_call_languages == ("en", "ja", "ar")
    monkeypatch.setenv("COPILOT_CALL_LANGUAGES", "en,unknown")
    with pytest.raises(ValidationError):
        core_settings()


@pytest.mark.parametrize("language", CALL_LANGUAGES)
def test_model_fallback_compatibility(language):
    assert recognition_supported(language, "nova-3", ["deepgram"])
    assert recognition_supported(language, "nova-3-general", ["deepgram"])
    if language != "en":
        assert not recognition_supported(language, "nova-3-medical", ["deepgram"])
        assert not recognition_supported(language, "nova-3", ["deepgram", "assemblyai"])


async def test_catalog_uses_provider_capability_and_retains_native_names():
    svc: Any = SimpleNamespace(settings=api_settings(stt_providers=["deepgram"]))
    result = await profiles.call_languages(svc, Principal(uuid4(), "Fixture", None))
    assert len(result["items"]) == 24
    assert next(i for i in result["items"] if i["id"] == "ar")["direction"] == "rtl"
    svc.settings.stt_providers = ["deepgram", "assemblyai"]
    assert [
        i["id"]
        for i in (await profiles.call_languages(svc, Principal(uuid4(), "Fixture", None)))["items"]
    ] == ["en"]


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c["language"])
def test_multilingual_grounding_preserves_source_and_original_script(case):
    matching = routes.source(
        "resume", "original", "Acme PostgreSQL", case["evidence"], "2026-11-20"
    )
    unrelated = routes.source(
        "fact", "irrelevant", "Oranges", "Fruit harvested yesterday", "2026-11-20"
    )
    assert ask.rank(case["question"], [unrelated, matching])[0] == matching
    assert ask.terms(case["evidence"])
    request = ask.request(case["question"], [], [matching])
    assert isinstance(request.content, str)
    payload = json.loads(request.content)
    assert payload["sources"][0]["content"] == case["evidence"]
    assert payload["sources"][0]["citation"] == "S1"
    assert "current question" in request.system[0].text


@pytest.mark.parametrize(
    "text", ["数据库延迟", "データベースの遅延", "ฐานข้อมูล", "डेटाबेस", "ডেটাবেস", "தரவுத்தள", "ڈیٹابیس"]
)
def test_script_aware_terms_keep_non_latin_text(text):
    assert ask.terms(text)
    assert ask.rank(text, [routes.source("resume", "x", "", text, "today")])


async def test_semantic_fallback_only_selects_relevant_bounded_sources():
    candidates = [
        routes.source("transcript", str(i), "Update", "unrelated text", str(i)) for i in range(80)
    ]
    resume = routes.source("resume", "resume", "Original resume", "Reduced database latency", "1")
    candidates.append(resume)

    class Embedder:
        model_id = "fixture"

        async def embed(self, texts):
            assert len(texts) <= 33
            assert all(len(t) <= 1651 for t in texts)
            return [
                [1.0, 0.0],
                *([1.0, 0.0] if "database" in text else [0.0, 1.0] for text in texts[1:]),
            ]

    assert await ask.semantic_rank(
        "データベースの待ち時間を減らした経験", candidates, Embedder()
    ) == [resume]
    assert ask.rank("データベースの待ち時間を減らした経験", candidates) == []


async def test_semantic_provider_failure_never_includes_unrelated_private_records(monkeypatch):
    monkeypatch.setattr(embed, "make_embedder", MagicMock(side_effect=RuntimeError("no provider")))
    sources = [routes.source("resume", "x", "Travel", "Travel to Rome", "today")]
    svc: Any = SimpleNamespace(settings=api_settings())
    assert await routes.rank_sources(svc, "データベースの待ち時間", sources) == []


@pytest.mark.parametrize("language", CALL_LANGUAGES)
async def test_practice_stt_wires_every_language_and_does_not_claim_unknown_voice(
    monkeypatch, language
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(
            200, json={"results": {"channels": [{"alternatives": [{"transcript": "fixture"}]}]}}
        )

    original = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kw: original(transport=httpx2.MockTransport(handler), **kw)
    )
    svc: Any = SimpleNamespace(settings=api_settings(deepgram_api_key=SecretStr("fixture")))
    assert (
        await practice.voice_request(svc, "transcription", b"mic", "audio/wav", language)
        == b"fixture"
    )
    assert seen[0].url.params["language"] == CALL_LANGUAGES[language].deepgram_code
    if language not in svc.settings.practice_tts_models:
        with pytest.raises(HTTPException, match="503"):
            await practice.voice_request(svc, "speech", b"", "", language, "question")
        assert len(seen) == 1


async def test_private_brief_preview_uses_account_and_profile_scopes(monkeypatch):
    account = Principal(uuid4(), "Account", None)
    profile = Principal(uuid4(), "Profile", None)
    connection = object()
    used = []

    @asynccontextmanager
    async def tx(principal):
        used.append(principal.user_id)
        yield connection

    svc: Any = SimpleNamespace(tx=tx, crypto=object())
    brief = {
        "id": "brief",
        "title": "Private",
        "content": "Approved context",
        "prepared_at": "2026-11-20",
    }
    latest = AsyncMock(return_value=[brief])
    monkeypatch.setattr(personal_ai, "latest_briefs", latest)
    monkeypatch.setattr(routes, "retrieve", AsyncMock(return_value=[]))
    preview = await practice.preview_context(svc, profile, account)
    assert preview["sources"][0]["kind"] == "private_brief"
    assert preview["sources"][0]["excerpt"] == "Approved context"
    assert latest.await_args is not None
    assert latest.await_args.args[2:4] == (account.user_id, profile.user_id)
    assert used == [profile.user_id, account.user_id]
    latest.return_value = []
    assert (await practice.preview_context(svc, profile, account))["sources"] == []


async def test_practice_keyterms_preserve_names_without_transcribing_interviewer(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(
            200, json={"results": {"channels": [{"alternatives": [{"transcript": "candidate"}]}]}}
        )

    original = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kw: original(transport=httpx2.MockTransport(handler), **kw)
    )
    svc: Any = SimpleNamespace(settings=api_settings(deepgram_api_key=SecretStr("fixture")))
    hints = practice.practice_keyterms("AcmeでPostgreSQLの性能をどう改善しましたか", [])
    assert hints == ["Acme", "PostgreSQL"]
    await practice.voice_request(
        svc, "transcription", b"candidate mic", "audio/wav", "ja", "Interviewer text", hints
    )
    assert seen[0].content == b"candidate mic"
    assert seen[0].url.params.get_list("keyterm") == hints
    assert seen[0].url.params["redact_usage"] == "true"


def test_practice_capabilities_are_present_on_every_public_session_response():
    state = {"sources": [], "spoken_language": "vi", "elapsed_seconds": 0, "clock_started_at": None}
    svc: Any = SimpleNamespace(settings=api_settings(deepgram_api_key=SecretStr("fixture")))
    out = practice.public(uuid4(), state, svc)
    assert out["voice_available"] is True
    assert out["speech_available"] is False
    state["spoken_language"] = "ja"
    assert practice.public(uuid4(), state, svc)["speech_available"] is True
    svc.settings.deepgram_model = "nova-3-medical"
    assert practice.public(uuid4(), state, svc)["voice_available"] is False


async def test_companion_language_comes_from_retained_session_not_profile():
    from copilot_api.routes import companion

    account = Principal(uuid4(), "Account", None)
    sid = uuid4()
    svc: Any = SimpleNamespace(
        valkey=SimpleNamespace(
            get=AsyncMock(return_value=str(sid)),
            hgetall=AsyncMock(
                return_value={
                    "account_id": str(account.user_id),
                    "user_id": str(account.user_id),
                    "status": "active",
                    "spoken_language": "ja",
                }
            ),
        )
    )
    result = await companion.current_sessions(svc, account, account)
    assert result["items"][0]["spoken_language"] == "ja"
    svc.valkey.hgetall.return_value.pop("spoken_language")
    assert (await companion.current_sessions(svc, account, account))["items"][0][
        "spoken_language"
    ] is None


async def test_mismatched_voice_never_falls_back_to_english():
    svc: Any = SimpleNamespace(
        settings=api_settings(
            deepgram_api_key=SecretStr("fixture"), practice_tts_models={"vi": "aura-2-thalia-en"}
        )
    )
    state = {"sources": [], "spoken_language": "vi", "elapsed_seconds": 0, "clock_started_at": None}
    assert practice.public(uuid4(), state, svc)["speech_available"] is False
    with pytest.raises(HTTPException, match="503"):
        await practice.voice_request(svc, "speech", b"", "", "vi", "question")
