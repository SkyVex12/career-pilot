"""Synthetic card lifecycle cases: the judge must grade an answer the UI actually had."""

from copilot_evals.sensei_judge import collect_answers


def event(at, kind, qid, **fields):
    return {"at": at, "message": {"type": kind, "question_id": qid, **fields}}


def detection(at, qid, text="Explain query plans", *, speculative=False):
    return event(
        at,
        "question.detected",
        qid,
        text=text,
        question_type="technical",
        speculative=speculative,
    )


def delta(at, qid, text, stage="fast"):
    return event(at, "answer.delta", qid, text=text, **{"pass": stage})


def collect(events, turns=None):
    turns = turns or [
        {"id": 1, "question": "Explain query plans", "suggestion": "Synthetic answer"}
    ]
    run = {
        "sent": [{"id": turn["id"], "at": index} for index, turn in enumerate(turns)],
        "events": events,
    }
    return collect_answers(run, turns)


def test_cancelled_speculation_does_not_contaminate_replacement():
    rows = collect(
        [
            detection(0.1, "withdrawn", speculative=True),
            delta(0.2, "withdrawn", "Discarded answer"),
            event(0.3, "question.cancelled", "withdrawn"),
            detection(0.4, "replacement"),
            delta(0.5, "replacement", "The surviving answer"),
        ]
    )
    row = rows[0]
    assert row["detected"]
    assert not row["cancelled"]
    assert row["fast"] == "The surviving answer"
    assert row["selected_card"] == "replacement"
    assert row["latency"]["fast_first_s"] == 0.5
    assert row["cards"] == ["withdrawn", "replacement"]
    assert row["card_audit"][0]["fast"] == "Discarded answer"
    assert row["card_audit"][0]["cancelled"]
    assert "multiple_cards" in row["attribution_ambiguities"]


def test_multiple_surviving_cards_choose_first_without_concatenation():
    row = collect(
        [
            detection(0.1, "first"),
            delta(0.2, "first", "First fast"),
            delta(0.3, "first", "First deep", "deep"),
            detection(0.4, "second"),
            delta(0.5, "second", "Second fast"),
            delta(0.6, "second", "Second deep", "deep"),
        ]
    )[0]
    assert row["selected_card"] == "first"
    assert row["fast"] == "First fast"
    assert row["deep"] == "First deep"
    assert row["card_audit"][1]["fast"] == "Second fast"


def test_empty_first_card_does_not_hide_later_answer():
    row = collect(
        [
            detection(0.1, "empty"),
            event(0.2, "answer.error", "empty", code="timeout", **{"pass": "fast"}),
            detection(0.3, "answered"),
            delta(0.4, "answered", "Usable answer"),
        ]
    )[0]
    assert row["selected_card"] == "answered"
    assert row["fast"] == "Usable answer"
    assert row["errors"] == []
    assert row["card_audit"][0]["errors"] == [{"stage": "fast", "code": "timeout"}]


def test_only_cancelled_card_retains_audit_but_has_no_graded_answer():
    row = collect(
        [
            detection(0.1, "withdrawn", speculative=True),
            delta(0.2, "withdrawn", "Withdrawn answer"),
            event(0.3, "question.cancelled", "withdrawn"),
        ]
    )[0]
    assert not row["detected"]
    assert row["cancelled"]
    assert row["selected_card"] is None
    assert row["fast"] == row["deep"] == ""
    assert row["card_audit"][0]["fast"] == "Withdrawn answer"


def test_extended_confirmation_moves_whole_card_to_latest_turn():
    turns = [
        {"id": 1, "question": "Explain query plans", "suggestion": "Synthetic answer"},
        {"id": 2, "question": "Include indexes", "suggestion": "Synthetic answer"},
    ]
    rows = collect(
        [
            detection(0.1, "q", speculative=True),
            delta(0.2, "q", "First portion. "),
            detection(1.1, "q", "Explain query plans. Include indexes"),
            delta(1.2, "q", "Second portion."),
        ],
        turns,
    )
    assert not rows[0]["detected"]
    assert rows[0]["fast"] == ""
    assert rows[1]["fast"] == "First portion. Second portion."
    assert rows[1]["cards"] == ["q"]
    assert set(rows[1]["attribution_ambiguities"]) == {
        "merged_turns",
        "card_reassigned",
        "multiple_source_matches",
    }


def test_empty_delta_does_not_start_first_word_latency():
    row = collect([detection(0.1, "q"), delta(0.2, "q", ""), delta(0.4, "q", "Answer")])[0]
    assert row["latency"]["fast_first_s"] == 0.4


def test_partial_word_match_is_not_credited():
    rows = collect(
        [detection(0.1, "q", "Explain indexes"), delta(0.2, "q", "Answer")],
        [{"id": 1, "question": "Explain index", "suggestion": "Synthetic answer"}],
    )
    assert not rows[0]["detected"]
    assert rows[0]["unmatched_card_count"] == 1
