"""Synthetic review timing and question-clustered reliability; no provider calls."""

from copy import deepcopy
from typing import Any, cast

import pytest
from copilot_evals.live_answer_eval import (
    Run,
    latency_components,
    metrics,
    paired_complete_cases,
    paired_stream_cases,
    row_id,
    summarize,
)
from copilot_evals.live_answer_stats import bootstrap_scope, clustered_rate, paired_uncertainty


def record(label="A", *, repetitions=1, questions=2):
    rows, sent, events, logs = [], [], [], []
    for question in range(questions):
        for rep in range(repetitions):
            origin = len(rows) * 10.0
            identifier, qid = f"q{question}:r{rep}", f"{label}-q{question}-r{rep}"
            rows.append(
                {
                    "id": identifier,
                    "original_id": f"q{question}",
                    "rep": rep,
                    "question": f"Question {question}?",
                    "type": "technical",
                    "category": "technical",
                    "needs_answer": True,
                    "status": "completed",
                    "fast": "A useful answer. ",
                    "deep": "",
                    "cards": [qid],
                    "selected_card": qid,
                    "latency": {"fast_first_s": 0.1, "fast_done_s": 0.3},
                }
            )
            sent.append({"id": identifier, "at": origin})
            for delta, kind, text in (
                (0.1, "answer.delta", "A useful"),
                (0.2, "answer.delta", " answer. "),
                (0.3, "answer.done", ""),
            ):
                events.append(
                    {
                        "at": origin + delta,
                        "message": {"type": kind, "question_id": qid, "pass": "fast", "text": text},
                    }
                )
            logs.append(
                {
                    "kind": "answer",
                    "question_id": qid,
                    "pass": "fast",
                    "latency_ms": {"model_dispatch_ms": 20, "router_first_chunk_ms": 50},
                }
            )
    return Run(
        label,
        rows,
        {"sent": sent, "events": events, "metadata_logs": logs, "speed": 1},
        {"condition_id": label},
        {"source": "same"},
        {"profile": "same"},
        [],
    )


def test_strict_cue_and_sentence_stages_do_not_relabel_deprecated_cue_aliases():
    run = record()
    judgment = {
        "first_useful_cue": "fast:1",
        "first_useful_complete_sentence": "fast:1",
        "unit_grammar": {"fast:1": True},
    }
    stages = latency_components(run, run.rows[0], judgment)
    assert stages["first_useful_after_first_emission_ms"] is None
    assert stages["first_useful_after_router_first_chunk_ms"] is None
    for label in ("useful_cue", "useful_complete_sentence"):
        assert stages[label + "_after_first_emission_ms"] == pytest.approx(100)
        assert stages[label + "_after_router_first_chunk_ms"] == pytest.approx(130)
    judgment["unit_grammar"]["fast:1"] = False
    stages = latency_components(run, run.rows[0], judgment)
    assert stages["useful_cue_after_first_emission_ms"] == pytest.approx(100)
    assert stages["useful_complete_sentence_after_first_emission_ms"] is None
    legacy = latency_components(run, run.rows[0], {"first_useful_sentence": 1})
    assert legacy["first_useful_after_first_emission_ms"] == pytest.approx(100)
    assert legacy["useful_cue_after_first_emission_ms"] is None


def test_failed_primary_successful_deep_keeps_strict_timing_and_its_router_origin():
    left, right = record("A", questions=1), record("D", questions=1)
    row = left.rows[0]
    row.update(
        fast="",
        deep="I recovered. ",
        failure="RuntimeError",
        status="failed",
        errors=[{"stage": "fast", "code": "timeout"}],
        latency={"deep_first_s": 2.0, "deep_done_s": 2.2},
    )
    qid = row["selected_card"]
    left.replay["events"] = [
        {"at": 0.5, "message": {"type": "answer.error", "question_id": qid, "pass": "fast"}},
        {
            "at": 2.0,
            "message": {
                "type": "answer.delta",
                "question_id": qid,
                "pass": "deep",
                "text": "I recovered. ",
            },
        },
        {"at": 2.2, "message": {"type": "answer.done", "question_id": qid, "pass": "deep"}},
    ]
    left.replay["metadata_logs"].append(
        {
            "kind": "answer",
            "question_id": qid,
            "pass": "deep",
            "latency_ms": {"model_dispatch_ms": 900, "router_first_chunk_ms": 1100},
        }
    )
    selected = {
        "first_useful_cue": "deep:1",
        "first_useful_complete_sentence": "deep:1",
        "unit_grammar": {"deep:1": True},
    }
    judgments = {
        "A": {row_id(row): selected},
        "D": {
            row_id(right.rows[0]): {
                "first_useful_complete_sentence": "fast:1",
                "unit_grammar": {"fast:1": True},
            }
        },
    }
    assert metrics(left, row, selected)["first_useful_complete_sentence_ms"] == 2000
    stages = latency_components(left, row, selected)
    assert stages["useful_complete_sentence_after_first_emission_ms"] == 0
    assert stages["useful_complete_sentence_after_router_first_chunk_ms"] == 0
    stream = paired_stream_cases(left, right, judgments)
    group = stream["all_started"]["groups"]["all"]
    assert group["latency_ms"]["first_useful_complete_sentence_ms"]["paired_observed"] == 1
    assert stream["primary_available"]["groups"]["all"]["included_pairs"] == 0
    assert paired_complete_cases(left, right, judgments)["groups"]["all"]["completed_pairs"] == 0


def test_identical_repetitions_do_not_narrow_question_cluster_rate_intervals():
    single = [("q0", False), ("q1", True), ("q2", True), ("q3", False)]
    repeated = [item for item in single for _ in range(4)]
    one, four = clustered_rate(single), clustered_rate(repeated)
    assert one["rate"] == four["rate"] == 0.5
    assert one["question_clustered"]["rate_95"] == four["question_clustered"]["rate_95"]
    assert four["question_clustered"]["unique_questions"] == 4
    assert four["question_clustered"]["observed_trials"] == 16
    assert one["wilson_95"] != four["wilson_95"]
    assert "repeated questions" in four["caveat"]
    differences = [("q0", -1.0), ("q1", 1.0)]
    assert (
        paired_uncertainty(differences)["mean_difference_95"]
        == paired_uncertainty([item for item in differences for _ in range(4)])[
            "mean_difference_95"
        ]
    )


def test_missing_and_single_question_scopes_have_no_invented_cluster_interval():
    empty = clustered_rate([])
    assert empty["rate"] is None
    assert empty["question_clustered"]["rate_95"] is None
    one = clustered_rate([("only", True)] * 4)
    assert one["question_clustered"]["unique_questions"] == 1
    assert one["question_clustered"]["rate_95"] is None
    with pytest.raises(ValueError, match="positive integer"):
        paired_uncertainty([], replicates=0)
    with pytest.raises(ValueError, match="booleans"):
        clustered_rate(cast(Any, [("missing", None)]))


def test_reliability_denominator_is_both_started_not_successful_latency_pairs():
    left, right = record("A", repetitions=4), record("D", repetitions=4)
    for row in left.rows:
        if row["original_id"] == "q0":
            row.update(failure="timeout", errors=[{"stage": "fast"}], status="failed")
    # One deliberately unstarted candidate stays outside both-started reliability.
    right.rows[-1].update(status="not_started", fast="", latency={})
    group = paired_complete_cases(left, right)["groups"]["all"]
    comparison = group["full_answer_completion_comparison"]
    assert group["completed_pairs"] == 3
    assert comparison["started_pairs"] == 7
    assert comparison["not_started_either"] == 1
    assert comparison["baseline"]["successes"] == 3
    assert comparison["candidate"]["successes"] == 7
    assert comparison["candidate_minus_baseline"]["mean"] == pytest.approx(4 / 7)
    assert comparison["candidate_minus_baseline"]["uncertainty"]["unique_questions"] == 2
    assert comparison["candidate_minus_baseline"]["uncertainty"]["observed_pairs"] == 7
    stream = paired_stream_cases(left, right)["all_started"]["groups"]["all"]
    assert stream["full_answer_completion_comparison"] == comparison
    summary = summarize(right)["groups"]["all"]
    assert summary["success_rate_started"]["n"] == 7
    assert summary["completion_coverage_planned"]["n"] == 8
    assert summary["not_started"] == 1
    assert summary["success_rate_started"]["question_clustered"]["unique_questions"] == 2


def test_cluster_rate_cache_is_invocation_scoped_and_defensive(monkeypatch):
    import copilot_evals.live_answer_stats as stats

    original = stats._paired_uncertainty
    calls = []

    def count(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(stats, "_paired_uncertainty", count)
    observations = [("q1", True), ("q2", False)]
    with bootstrap_scope():
        initial = clustered_rate(observations, replicates=50)
        expected = deepcopy(initial)
        initial["question_clustered"]["rate_95"][0] = -100
        assert clustered_rate(observations, replicates=50) == expected
    assert len(calls) == 1
    assert clustered_rate(observations, replicates=50) == expected
    assert len(calls) == 2
