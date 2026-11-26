"""Gateway/native wire contract for a forced takeover's timing anchor."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from copilot_core.ids import uuid7
from copilot_gateway.answer import Question
from copilot_gateway.auth import Principal
from copilot_gateway.session import Session, SessionInfo, SessionManager
from copilot_gateway.turn import Detection

WIRE_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "desktop/core/tests/fixtures/forced_takeover_detected.json"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [True, False])
async def test_first_release_preserves_forced_anchor_contract(env: Any, forced: bool) -> None:
    expected = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    settings = env.settings(
        answer_architecture="adaptive",
        deep_pass_auto=False,
        prewarm_jobs=[],
        llm_warm_interval_s=None,
    )
    services = env.services(settings, stt_names=())
    manager = SessionManager(services)
    info = SessionInfo(
        mode="interview",
        job_id=None,
        title=None,
        platform="authored",
        client_version="test",
        store_transcript=True,
        started_at=datetime.now(UTC),
        no_call=True,
    )
    session = Session(
        services, manager, uuid7(), Principal(env.user_a, "Alex", None), info, None, None
    )
    q = Question(
        qid=UUID(expected["question_id"]),
        text=expected["text"],
        qtype="other",
        confidence=0,
        addressed=False,
        speculative=True,
        forced=False,
        t0=time.monotonic(),
        detector_ms=None,
        match=None,
        held=True,
    )
    # No model or network tasks: exercise the real release/emit/confirmation path
    # with one already-held chunk, then settle the authored classifier future.
    q.held_text.append("Ready to answer.")
    session.answers.questions[q.qid] = q
    detection: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
    typing: list[asyncio.Task[None]] = []
    try:
        if forced:
            session._force_taken(q.qid, q.text, detection, respond=False)
            typing = list(session._forced)
        else:
            session.release_question(q.qid, q.text, Detection("answer", "other"), speculative=False)
            expected["confidence"] = 0.9
        messages = [json.loads(raw) for _, raw in session.ring]
        assert messages[0] == expected
        assert messages[1]["type"] == "answer.delta"
        assert messages[1]["question_id"] == str(q.qid)
        assert messages[1]["sequence"] == 0
        assert messages[1]["text"] == "Ready to answer."
        assert q.forced is forced
        assert not q.held
        assert q.released.is_set()
        if forced:
            detection.set_result(Detection("answer", "technical", latency_ms=25))
            await asyncio.gather(*typing)
            assert q.confirmed
            assert q.qtype == "technical"
            assert q.confidence == 1.0
            assert (
                len(
                    [
                        json.loads(raw)
                        for _, raw in session.ring
                        if json.loads(raw)["type"] == "question.detected"
                    ]
                )
                == 1
            )
    finally:
        session.ending = True
        if not detection.done():
            detection.cancel()
        for task in typing:
            if not task.done():
                task.cancel()
        await asyncio.gather(*typing, return_exceptions=True)
        await session.answers.shutdown(within_s=1)
        await services.store.client.aclose()
