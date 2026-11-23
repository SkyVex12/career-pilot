"""Deterministic language contract and runtime checks; no live speech certification."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from copilot_core import keys, prompts
from copilot_core.ids import uuid7
from copilot_core.languages import CALL_LANGUAGES, validate_language
from copilot_gateway.auth import Principal
from copilot_gateway.session import Session, SessionInfo, SessionManager
from copilot_gateway.stt.deepgram import DeepgramSTT
from copilot_gateway.testing import GatewayServer, ProtocolClient
from copilot_gateway.turn import likely_question
from copilot_worker import prompts as worker_prompts
from copilot_worker.sessionlog import metadata_summary, render_outputs


@pytest.mark.parametrize("language", ["ES", "es-MX", "", "xx", "en\nignore"])
def test_unknown_language_is_rejected(language):
    with pytest.raises(ValueError, match="language"):
        validate_language(language)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("es", "Cuéntame sobre tu experiencia"),
        ("es", "Cómo resolviste ese problema"),
        ("vi", "Bạn giải quyết vấn đề này như thế nào"),
        ("vi", "Hãy giải thích thiết kế của bạn"),
    ],
)
def test_question_cues_do_not_require_english(language, text):
    assert likely_question(text, spoken_language=language)
    assert not likely_question("hola muchas gracias", spoken_language="es")


@pytest.mark.parametrize("language", CALL_LANGUAGES)
def test_every_prompt_uses_selected_language_and_preserves_evidence(language):
    ctx = prompts.PromptContext(
        "Alex", "interview", "[n:abcd] Acme", None, spoken_language=language
    )
    requests = [
        prompts.fast_pass(ctx, "", "question", None, 100),
        prompts.detector("Alex", "", "question", 100, spoken_language=language),
        prompts.summary("Alex", "interview", "", "text", 100, language),
        worker_prompts.postcall_request("interview", [], "text", "", spoken_language=language),
    ]
    for req in requests:
        system = " ".join(block.text for block in req.system)
        assert CALL_LANGUAGES[language].name in system
        assert "citation markers" in system
        assert "Never invent evidence" in system
    assert "[n:abcd] Acme" in " ".join(b.text for b in requests[0].system)
    decision = prompts.detector_decision("Alex", "", "question", spoken_language=language)
    assert CALL_LANGUAGES[language].name in str(decision.input)


@pytest.mark.parametrize("language", CALL_LANGUAGES)
async def test_snapshot_reused_for_every_recognition_factory(env, language):
    svc = env.services(env.settings(verified_call_languages=("en", language)))

    def factory(terms):
        return DeepgramSTT(
            "fake", url="wss://example.invalid/listen", model="nova-3", keyterms=list(terms)
        )

    svc.stt_providers = [("deepgram", factory), ("deepgram", factory)]
    svc.stt_me_providers = [("deepgram", factory)]
    manager = SessionManager(svc)
    manager._validate_recognition_language(language)
    info = SessionInfo(
        "interview", None, None, "test", "test", True, datetime.now(UTC), spoken_language=language
    )
    session = Session(svc, manager, uuid7(), Principal(env.user_a, "Alex", None), info, None, None)
    # A later profile/default change cannot alter a running session snapshot.
    info.spoken_language = "en"
    assert session.state_fields()["spoken_language"] == language
    assert session.prompt_context().spoken_language == language
    for stream in [session.stt, session.stt_me]:
        assert stream is not None
        for _, make in stream.providers:
            for _ in range(2):  # original connection, reconnect and each failover
                dg = make(["Acme"])
                assert isinstance(dg, DeepgramSTT)
                assert parse_qs(urlsplit(dg.url).query)["language"] == [
                    CALL_LANGUAGES[language].deepgram_code
                ]
                assert parse_qs(urlsplit(dg.url).query)["keyterm"] == ["Acme"]


async def test_unverified_language_rejected_before_claim_or_model(env):
    svc = env.services(env.settings(verified_call_languages=("en",)))
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        await client.connect()
        await client.send(
            {
                "type": "session.start",
                "mode": "interview",
                "platform": "test",
                "client_version": "test",
                "consent": {"store_transcript": True},
                "audio": {"sample_rate": 16000, "encoding": "pcm16"},
                "spoken_language": "es",
            }
        )
        error = await client.wait_type("error")
        assert "language" in error["message"]
        assert not gateway.manager.sessions
        assert await svc.store.client.get(keys.user_active_session(env.user_a)) is None
        await client.close()


def test_postcall_labels_and_unknown_legacy_language():
    data = {
        "summary": "Resumen [n:abcd]",
        "strengths": ["Acme"],
        "follow_up_email": {"subject": "Gracias", "body": "Hola"},
    }
    out = render_outputs("interview", data, "es")
    assert "Fortalezas" in out["feedback"]
    assert out["summary"] == data["summary"]
    assert out["follow_up_email"].startswith("Asunto:")
    assert "Bản chép lời" in metadata_summary("interview", 2, [], "vi")
    legacy = worker_prompts.postcall_request("interview", [], "text", "")
    assert "selected spoken call language" not in legacy.system[0].text


@pytest.mark.parametrize("language", [None, *CALL_LANGUAGES])
async def test_restore_uses_retained_language_not_current_default(env, monkeypatch, language):
    from typing import cast

    from starlette.websockets import WebSocket

    from copilot_protocol import models

    svc = env.services(env.settings(verified_call_languages=tuple(CALL_LANGUAGES)))
    svc.stt_providers = [
        (
            "deepgram",
            lambda _: DeepgramSTT("fake", url="wss://example.invalid/listen", model="nova-3"),
        )
    ]
    svc.stt_me_providers = svc.stt_providers
    sid = uuid7()
    state = {
        "user_id": str(env.user_a),
        "status": "active",
        "mode": "interview",
        "started_at": datetime.now(UTC).isoformat(),
    }
    if language:
        state["spoken_language"] = language
    await svc.store.client.hset(keys.sess_state(sid), mapping=state)
    attached = []

    async def attach(self, ws, last_seq, replay=None):
        attached.append(self.spoken_language)

    monkeypatch.setattr(Session, "attach", attach)
    monkeypatch.setattr(Session, "start_core", lambda self: None)
    manager = SessionManager(svc)
    await manager.resume(
        cast(WebSocket, None),
        Principal(env.user_a, "Alex", None),
        models.SessionResume(type="session.resume", session_id=sid, last_seq=0),
    )
    assert attached == [language or "en"]
    assert manager.sessions[sid].state_fields()["spoken_language"] == (language or "en")


CORPUS = json.loads(
    (Path(__file__).resolve().parents[3] / "tests/fixtures/call_languages.json").read_text("utf-8")
)


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c["language"])
def test_corpus_question_detection_and_postcall_output(case):
    language = case["language"]
    assert likely_question(case["question"], spoken_language=language)
    assert not likely_question(case["statement"], spoken_language=language)
    evidence = case["evidence"] + " [n:abcd]"
    rendered = render_outputs(
        "interview",
        {
            "summary": evidence,
            "strengths": [evidence],
            "follow_up_email": {"subject": "Acme", "body": evidence},
        },
        language,
    )
    assert rendered["summary"] == evidence
    assert evidence in rendered["feedback"]
    assert "[n:abcd]" in rendered["follow_up_email"]
    if language != "en":
        assert "Strengths" not in rendered["feedback"]
        assert "The transcript was not stored" not in metadata_summary(
            "interview", 2, ["technical"], language
        )


@pytest.mark.parametrize("language", CALL_LANGUAGES)
def test_known_language_available_by_default_with_operator_opt_out(language):
    assert validate_language(language) == language
    if language != "en":
        with pytest.raises(ValueError, match="not enabled"):
            validate_language(language, ("en",))
