"""Deterministic practice state machine and feedback integrity."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from copilot_core import practice


def active(**options):
    state = practice.new_state(practice.Setup(request_id=uuid4(), **options), [], "en")
    practice.apply_result(
        state,
        {
            "plan": ["Evidence", "Tradeoffs"],
            "question": "Describe a real project",
            "question_kind": "experience",
        },
    )
    return state


def action(name, **values):
    return practice.Action(request_id=uuid4(), action=name, **values)


def feedback(answer="I measured latency", score=2):
    return {
        "dimensions": dict.fromkeys(practice.RUBRIC, score),
        "strengths": ["Concrete action"],
        "improvements": ["Explain the outcome"],
        "excerpts": [answer],
        "uncertainty": ["Outcome not supplied"],
        "suggested_wording": "I measured latency; [supply outcome].",
    }


def turn(state, answer="I measured latency", question="What tradeoff did you make?"):
    assert practice.reduce(state, action("answer", answer=answer))
    practice.apply_result(
        state, {"feedback": feedback(answer), "question": question, "question_kind": "experience"}
    )


def test_plan_asks_one_question_and_keeps_sample_hidden():
    state = active()
    assert len(state["questions"]) == 1
    assert state["status"] == "answering"
    assert state["simulation"] is True
    assert practice.current(state)["sample"] is None
    assert practice.current(state)["hint"] is None


def test_imported_question_preserves_original_and_source():
    original = {"content": "How did you decide?", "id": "call:them:3", "start_ms": 42000}
    state = practice.new_state(practice.Setup(request_id=uuid4()), [], "en", original)
    practice.apply_result(state, {"plan": ["Decisions"], "question": "Different wording"})
    q = practice.current(state)
    assert q["text"] == original["content"]
    assert q["kind"] == "original"
    assert q["source"]["start_ms"] == 42000


def test_answer_is_saved_before_generation_and_followup_sees_actual_words():
    state = active()
    practice.reduce(state, action("answer", answer="I chose consistency over availability"))
    assert state["status"] == "processing"
    req = practice.request(state)
    assert isinstance(req.content, str)
    data = json.loads(req.content)
    assert data["questions"][0]["attempts"][0]["answer"] == "I chose consistency over availability"
    assert "untrusted data" in req.system[0].text
    assert "predict hiring success" in req.system[0].text
    assert "accent" in req.system[0].text


def test_pause_resume_keeps_completed_answers_and_excludes_paused_time():
    state = active()
    turn(state)
    state["running_since"] = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    assert not practice.reduce(state, action("pause"))
    frozen = state["elapsed_seconds"]
    assert 45 <= frozen < 50
    assert practice.elapsed(state, datetime.now(UTC) + timedelta(hours=1)) == frozen
    assert not practice.reduce(state, action("resume"))
    assert state["questions"][0]["attempts"][0]["feedback"]


def test_pause_during_processing_resume_retries_pending_operation_without_another_answer():
    state = active()
    practice.reduce(state, action("answer", answer="My example"))
    pending = dict(state["pending"])
    practice.reduce(state, action("pause"))
    assert practice.reduce(state, action("resume"))
    assert state["pending"] == pending
    assert len(practice.current(state)["attempts"]) == 1


def test_retry_preserves_attempts_and_records_prior_coaching_exposure():
    state = active()
    q = practice.current(state)
    practice.reduce(state, action("hint"))
    practice.apply_result(state, {"text": "Explain your decision"})
    turn(state)
    first = q["attempts"][0]
    practice.reduce(state, action("retry", question_id=UUID(q["id"])))
    assert q["sample"] is None
    assert q["assistance"] == ["prior_coaching"]
    turn(state, answer="I measured latency and chose a cache", question="What did the cache cost?")
    assert q["attempts"][0] == first
    assert len(q["attempts"]) == 2
    assert q["attempts"][1]["comparison"]["comparable"] is False
    assert q["attempts"][1]["feedback"]["rubric_version"] == practice.RUBRIC_VERSION


def test_skip_and_save_do_not_invent_a_user_answer():
    state = active()
    q = practice.current(state)
    assert not practice.reduce(state, action("save"))
    assert q["saved_for_later"]
    assert practice.reduce(state, action("skip"))
    practice.apply_result(
        state, {"question": "Design a hypothetical cache", "question_kind": "hypothetical"}
    )
    assert q["attempts"] == []
    assert q["skipped"] is True
    assert practice.current(state)["kind"] == "hypothetical"


def test_duration_finishes_after_completed_feedback():
    state = active(duration_minutes=3)
    state["elapsed_seconds"] = 181
    turn(state)
    assert state["status"] == "finished"
    assert len(state["questions"]) == 1
    assert state["questions"][0]["attempts"][0]["feedback"]


def test_unsupported_quotes_are_removed_and_scores_validated():
    raw = feedback("Invented quote")
    result = practice.validate_feedback(raw, "The actual answer")
    assert result["excerpts"] == []
    assert "unsupported" in result["uncertainty"][-1]
    raw["dimensions"]["relevance"] = 9
    with pytest.raises(practice.PracticeError, match="rubric scores"):
        practice.validate_feedback(raw, "answer")


@pytest.mark.parametrize("name", ["answer", "skip", "hint", "sample"])
def test_paused_session_rejects_answer_processing(name):
    state = active()
    practice.reduce(state, action("pause"))
    with pytest.raises(practice.PracticeError, match="resume"):
        practice.reduce(state, action(name, answer="text"))


def test_stale_question_answer_and_repeated_generation_rejected():
    state = active()
    with pytest.raises(practice.PracticeError, match="active question"):
        practice.reduce(state, action("answer", question_id=uuid4(), answer="answer"))
    practice.reduce(state, action("skip"))
    with pytest.raises(practice.PracticeError, match="repeated"):
        practice.apply_result(state, {"question": state["questions"][0]["text"]})


def test_finish_clears_pending_and_raw_audio_retention_is_explicitly_disabled():
    state = active()
    practice.reduce(state, action("answer", answer="answer"))
    practice.reduce(state, action("finish"))
    assert state["pending"] is None
    assert state["running_since"] is None
    with pytest.raises(ValidationError):
        practice.Setup.model_validate({"request_id": str(uuid4()), "retain_audio": True})
