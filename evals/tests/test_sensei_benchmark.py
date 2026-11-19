"""Synthetic benchmark tests: budget, blinding, attribution and calibration."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from copilot_evals.sensei_judge import RUBRIC, aggregate, collect_answers, orders, validate_grade
from copilot_evals.sensei_report import calibration, overlap_check
from copilot_evals.sensei_runtime import Budget, MeteredProvider, private_directory, save

from copilot_core.llm import LLMDone, LLMUsage
from copilot_core.pricing import ModelPrice


def grade():
    score = {
        "initial": [4] * 6,
        "expanded": [3] * 6,
        "invented_initial": 0,
        "invented_expanded": 1,
        "unsupported": ["Unverified team-size claim"],
    }
    return {
        "A": deepcopy(score),
        "B": deepcopy(score),
        "preference": "A",
        "expanded_preference": "tie",
        "reason": "More direct",
        "expanded_reason": "Equal",
    }


def test_blind_orders_reverse_and_scores_follow_identity():
    assignment = orders([1, 2, 3])
    assert assignment == orders([1, 2, 3])
    assert all(a == b[::-1] for a, b in assignment.values())
    first, second = grade(), grade()
    first["B"]["initial"] = [2] * 6
    second["A"]["initial"] = [2] * 6
    second["preference"] = "B"
    result = aggregate(
        [{"id": 1, "type": "technical", "needs_answer": True}],
        [
            {"id": 1, "order": ["ours", "sensei"], "grade": first},
            {"id": 1, "order": ["sensei", "ours"], "grade": second},
        ],
    )["questions"][0]
    assert result["ours"]["initial"] == [4] * 6
    assert result["sensei"]["initial"] == [2] * 6
    assert result["preference"] == "ours"


def test_position_bias_is_disagreement_not_tie():
    result = aggregate(
        [{"id": 1, "type": "technical", "needs_answer": True}],
        [
            {"id": 1, "order": ["ours", "sensei"], "grade": grade()},
            {"id": 1, "order": ["sensei", "ours"], "grade": grade()},
        ],
    )["questions"][0]
    assert result["preference"] == "disagreement"


@pytest.mark.parametrize("bad", [0, 6, True, 2.5, "4"])
def test_invalid_scores_rejected(bad):
    value = grade()
    value["A"]["initial"][0] = bad
    with pytest.raises(ValueError, match="six integers"):
        validate_grade(value)


def test_delayed_answer_attributed_to_question_not_latest_input():
    turns = [
        {"id": 1, "question": "Explain query plans", "suggestion": "Synthetic answer"},
        {"id": 2, "question": "What about indexes", "suggestion": "Synthetic answer"},
    ]

    def event(at, type_, **fields):
        return {"at": at, "message": {"type": type_, **fields}}

    run = {
        "sent": [{"id": 1, "at": 0}, {"id": 2, "at": 1}],
        "events": [
            event(
                0.5,
                "question.detected",
                question_id="q1",
                text="Explain query plans",
                question_type="technical",
                speculative=True,
            ),
            event(
                1.5,
                "question.detected",
                question_id="q1",
                text="Explain query plans",
                question_type="technical",
                speculative=False,
            ),
            event(2, "answer.delta", question_id="q1", text="A query plan", **{"pass": "fast"}),
            event(
                3,
                "answer.done",
                question_id="q1",
                memory_refs=[{"id": "source"}],
                latency_ms={"detector_ms": 200},
                source="model",
                **{"pass": "fast"},
            ),
        ],
    }
    rows = collect_answers(run, turns)
    assert rows[0]["fast"] == "A query plan"
    assert rows[0]["cards"] == ["q1"]
    assert rows[0]["latency"]["detected_s"] == 0.5
    assert rows[0]["latency"]["confirmed_s"] == 1.5
    assert rows[0]["citations"] == [{"id": "source"}]
    assert rows[1]["fast"] == ""
    assert not rows[1]["detected"]


def test_budget_reserves_across_restarts_and_rejects_unknown_price(tmp_path):
    settings = SimpleNamespace(
        model_prices={"synthetic": ModelPrice(input=1, cached=0.1, output=5)},
        tier_price_multipliers={"standard": 1},
    )
    budget = Budget(tmp_path, settings, limit=0.01)
    ticket = budget.reserve("synthetic", "data", 100)
    restored = Budget(tmp_path, settings, limit=0.01)
    assert restored.total == budget.total
    with pytest.raises(ValueError, match="budget exhausted"):
        restored.reserve("synthetic", "x" * 10000, 10000)
    with pytest.raises(ValueError, match="price missing"):
        restored.reserve("unpriced", "x", 1)
    budget.finish(ticket, LLMDone(LLMUsage(100, 0, 10), "done", "test", "synthetic"))
    assert budget.total == pytest.approx(0.00015)


def test_only_verified_pre_submission_failure_releases_reservation(tmp_path):
    settings = SimpleNamespace(
        model_prices={"synthetic": ModelPrice(input=1, cached=0.1, output=5)},
        tier_price_multipliers={"standard": 1},
    )
    budget = Budget(tmp_path, settings)
    ticket = budget.reserve("synthetic", "data", 100)
    provider = MeteredProvider(SimpleNamespace(name="test"), budget)
    provider.connection_failure(ticket, TimeoutError())
    assert budget.total > 0
    error = ConnectionError()
    error.__cause__ = type("ConnectTimeout", (Exception,), {})()
    provider.connection_failure(ticket, error)
    assert budget.total == 0


def test_private_output_cannot_be_in_repository():
    from pathlib import Path

    with pytest.raises(ValueError, match="outside the repository"):
        private_directory(Path(__file__).resolve().parents[2])


def test_verbatim_publication_is_rejected():
    source = "one two three four five six seven eight nine ten eleven"
    with pytest.raises(ValueError, match="verbatim"):
        overlap_check(source, [{"question": source, "suggestion": "short"}])
    overlap_check("A completely rewritten summary", [{"question": source, "suggestion": "short"}])


def test_empty_calibration_is_never_trusted(tmp_path):
    save(
        tmp_path / "human-review.json",
        [
            {
                "id": 1,
                "preference": None,
                "A": {"scores": dict.fromkeys(RUBRIC)},
                "B": {"scores": dict.fromkeys(RUBRIC)},
            }
        ],
    )
    result = calibration(tmp_path, {"questions": []}, [])
    assert result["completed"] == 0
    assert result["status"] == "unvalidated"
    assert result["preference_agreement"] is None


def test_input_hash_rejects_changed_export(tmp_path):
    from copilot_evals.sensei_benchmark import bind_inputs

    export = tmp_path / "source.txt"
    export.write_text("#1\nNov 19, 10:00:00 AM\nSynthetic question\nSynthetic answer\n", "utf-8")
    (tmp_path / "resume.txt").write_text("Synthetic resume", "utf-8")
    (tmp_path / "jd.txt").write_text("Synthetic JD", "utf-8")
    bind_inputs(tmp_path, export, 2026)
    export.write_text("#1\nNov 19, 10:00:00 AM\nDifferent question\nSynthetic answer\n", "utf-8")
    with pytest.raises(ValueError, match="Sources changed"):
        bind_inputs(tmp_path, export, 2026)


def test_existing_cache_cannot_be_adopted_for_different_sources(tmp_path):
    from copilot_evals.sensei_benchmark import bind_inputs

    export = tmp_path / "source.txt"
    export.write_text("#1\nNov 19, 10:00:00 AM\nSynthetic question\nSynthetic answer\n", "utf-8")
    (tmp_path / "resume.txt").write_text("Synthetic resume", "utf-8")
    (tmp_path / "jd.txt").write_text("Synthetic JD", "utf-8")
    save(tmp_path / "turns.json", [])
    with pytest.raises(ValueError, match="Export differs"):
        bind_inputs(tmp_path, export, 2026)
