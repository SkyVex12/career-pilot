from typing import Any

from copilot_evals.live_answer_session_replay import _prepared_violations as prepared_violations


def event(type_: str, **kwargs: Any) -> dict[str, Any]:
    return {"message": {"type": type_, **kwargs}}


def detected(qid):
    return event("question.detected", question_id=qid)


def done(qid, source):
    return event("answer.done", question_id=qid, source=source, **{"pass": "fast"})


def update():
    return event("transcript.final", channel="me")


def check(events, **kwargs):
    return prepared_violations(
        events, stale_source=kwargs.get("stale_source", False), legacy=kwargs.get("legacy", False)
    )


def test_old_global_sources_oracle_falsely_flags_new_question_failure():
    events = [
        detected("old"),
        done("old", "prepared"),
        update(),
        detected("new"),
        event("answer.error", question_id="new"),
    ]
    sources = [e["message"].get("source") for e in events if e["message"]["type"] == "answer.done"]
    assert sources[-1:] == ["prepared"]  # Frozen oracle flags this.
    assert check(events) == []  # No prepared completion belongs to the failed new qid.


def test_new_question_prepared_completion_is_flagged():
    events = [
        detected("old"),
        done("old", "prepared"),
        update(),
        detected("new"),
        done("new", "prepared"),
    ]
    assert check(events) == ["stale_prepared_after_source_or_context_update"]


def test_new_question_fresh_model_completion_passes():
    assert (
        check(
            [
                detected("old"),
                done("old", "prepared"),
                update(),
                detected("new"),
                done("new", "model"),
            ]
        )
        == []
    )


def test_late_old_completion_is_not_reattributed_to_new_question():
    assert (
        check(
            [
                detected("old"),
                update(),
                detected("new"),
                done("old", "prepared"),
                done("new", "model"),
            ]
        )
        == []
    )


def test_missing_initial_prepared_is_retained():
    assert check([detected("old"), done("old", "model")]) == ["missing_initial_prepared_fast_path"]


def test_stale_source_fixture_rejects_any_prepared():
    assert check([detected("old"), done("old", "prepared")], stale_source=True) == [
        "stale_prepared_after_source_or_context_update"
    ]


def test_legacy_fixture_preserves_existing_baseline_exemption():
    assert check([detected("old"), done("old", "prepared")], stale_source=True, legacy=True) == []


def test_event_order_wins_over_identical_rounded_timestamps():
    events = [
        detected("old"),
        done("old", "prepared"),
        update(),
        detected("new"),
        done("new", "prepared"),
    ]
    for item in events:
        item["at_ms"] = 10
    assert check(events) == ["stale_prepared_after_source_or_context_update"]
