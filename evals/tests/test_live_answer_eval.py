"""Offline comparison guards: attribution, missing telemetry, cost provenance, blinding."""

from copy import deepcopy
from typing import Any

import pytest
from copilot_evals.live_answer_eval import (
    Run,
    blind_pairs,
    boundary_units,
    comparable,
    distribution,
    judgment_rates,
    ledger_summary,
    lexical_overlap,
    load_run,
    main,
    metrics,
    paired_complete_cases,
    paired_stream_cases,
    preferences,
    question_usage,
    read_judgments,
    sentence_events,
    summarize,
)
from copilot_evals.sensei_runtime import save


def run(architecture="A"):
    row = {
        "id": 1,
        "question": "Explain an index.",
        "type": "technical",
        "needs_answer": True,
        "fast": "It speeds up a lookup. It has a write cost.",
        "deep": "A B-tree supports ordered access and costs write maintenance.",
        "cards": ["cancelled", "current"],
        "selected_card": "current",
        "latency": {"fast_first_s": 0.1, "fast_done_s": 0.8, "deep_done_s": 1.5},
    }
    events = [
        {
            "at": 1.01,
            "message": {
                "type": "answer.delta",
                "question_id": "cancelled",
                "pass": "fast",
                "text": "Wrong card.",
            },
        },
        {
            "at": 1.1,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "It speeds up a lookup.",
            },
        },
        {
            "at": 1.4,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": " It has a write cost.",
            },
        },
        {"at": 1.8, "message": {"type": "answer.done", "question_id": "current", "pass": "fast"}},
    ]
    return Run(
        architecture,
        [row],
        {"sent": [{"id": 1, "at": 1}], "events": events, "speed": 1},
        {"settings": {"answer_architecture": architecture}, "code": {"engine": architecture}},
        {"source_sha256": "same-source"},
        {"resume_source": "Fictional database engineer", "jd_source": "Fictional role"},
        [],
    )


def test_quantiles_count_samples_and_never_invent_missing_zeroes():
    result = distribution([100, 200, None, float("nan"), -1, True])
    assert result["n"] == 2
    assert result["missing"] == 4
    assert result["p50"] == 150
    assert result["p90"] == 190
    assert result["p95"] == 195
    assert distribution([])["p95"] is None


def test_lexical_overlap_does_not_assign_semantic_redundancy():
    result = lexical_overlap("It is safe.", "It is not safe.")
    assert result["shared_token_fraction_of_initial"] == 1
    assert result["semantic_redundancy"] is None


def test_stream_timing_uses_selected_card_and_waits_for_sentence_boundary():
    record = run()
    sentences = sentence_events(record, record.rows[0])
    assert [item["text"] for item in sentences] == [
        "It speeds up a lookup.",
        "It has a write cost.",
    ]
    assert sentences[0]["ms"] == pytest.approx(400)
    assert sentences[1]["ms"] == pytest.approx(800)
    measured = metrics(record, record.rows[0], {})
    assert measured["first_useful_sentence_ms"] is None
    assert measured["complete_ms"] == 1500
    assert metrics(record, record.rows[0], {"first_useful_sentence": 2})[
        "first_useful_sentence_ms"
    ] == pytest.approx(800)


def test_multiple_sentences_in_one_chunk_are_not_cumulative():
    record = run()
    record.replay["events"] = [
        {
            "at": 1.2,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "First sentence. Second sentence. ",
            },
        },
    ]
    assert [item["text"] for item in sentence_events(record, record.rows[0])] == [
        "First sentence.",
        "Second sentence.",
    ]


def test_only_explicit_sequence_ids_deduplicate_chunks():
    record = run()
    event = {
        "at": 1.2,
        "message": {
            "type": "answer.delta",
            "question_id": "current",
            "pass": "fast",
            "text": "One. ",
            "sequence": 0,
        },
    }
    record.replay["events"] = [event, deepcopy(event)]
    assert len(sentence_events(record, record.rows[0])) == 1


def test_done_without_stream_does_not_create_sentence_latency():
    record = run()
    record.replay["events"] = record.replay["events"][-1:]
    assert metrics(record, record.rows[0], {})["first_complete_sentence_ms"] is None


def test_failed_expert_is_not_reported_as_completed():
    record = run()
    record.rows[0]["deep"] = ""
    record.rows[0]["latency"].pop("deep_done_s")
    record.rows[0]["errors"] = [{"stage": "deep", "code": "timeout"}]
    assert metrics(record, record.rows[0], {})["complete_ms"] is None


def test_semantic_rates_count_reviewed_answers_and_keep_unknowns_missing():
    record = run()
    other = deepcopy(record.rows[0])
    other["id"] = 2
    result = judgment_rates(
        [*record.rows, other], {"1:0": {"redundant": False, "meaningfully_adds": True}}
    )
    assert result["redundant"] == {
        "positive": 0,
        "reviewed": 1,
        "eligible": 2,
        "missing": 1,
        "percent_of_reviewed": 0,
    }
    assert result["meaningfully_adds"]["percent_of_reviewed"] == 100
    assert result["incorrect"]["percent_of_reviewed"] is None


def test_comparison_requires_fixed_sources_questions_profile_and_labels():
    left, right = run(), run("B")
    assert comparable(left, right)["comparable"]
    right.rows[0]["question"] = "Different question"
    assert not comparable(left, right)["comparable"]
    right = run("B")
    right.profile["resume_source"] = "Historical answer inserted here"
    assert not comparable(left, right)["comparable"]
    right = run("B")
    right.rows[0]["needs_answer"] = False
    assert not comparable(left, right)["comparable"]
    right = run("B")
    right.inputs = {}
    with pytest.raises(ValueError, match="identical source"):
        blind_pairs([left, right], 10, 1)


def test_blind_pairs_are_deterministic_and_identity_key_is_separate():
    records = [run(), run("B")]
    records[1].rows[0]["deep"] = ""
    review, key = blind_pairs(records, 10, 11)
    assert (review, key) == blind_pairs(records, 10, 11)
    assert "architecture" not in str(review)
    item = review["items"][0]
    for side in ("A", "B"):
        assert set(item[side]) == set(item["A"])
        item[side]["redundant"] = False
    judgments = read_judgments(review, key)
    assert judgments["A"]["1:0"]["redundant"] is False
    assert judgments["B"]["1:0"]["redundant"] is False
    item["preference"] = "A"
    winner = key["pairs"][item["pair_id"]]["assignment"]["A"]["architecture"]
    assert preferences(review, key) == {"A:B": {winner: 1}}
    item["A"]["incorrect"] = "no"
    with pytest.raises(ValueError, match="true, false"):
        read_judgments(review, key)


def test_review_cannot_silently_change_evidence_or_answers():
    review, key = blind_pairs([run(), run("B")], 10, 11)
    review["items"][0]["A"]["initial"] = "Replacement answer."
    with pytest.raises(ValueError, match="content changed"):
        read_judgments(review, key)


def test_ledger_reservations_are_not_actual_usage_or_per_question_cost():
    result = ledger_summary(
        [
            {"model": "test", "status": "usage", "charged_usd": 0.02},
            {"model": "test", "status": "reserved", "charged_usd": 0.10},
        ]
    )
    assert result["provider_usage_priced_usd"] == 0.02
    assert result["unresolved_reserved_upper_usd"] == 0.10
    assert result["per_question_cost"] is None


def test_actual_and_estimated_usage_do_not_mix_and_cancelled_cards_are_charged():
    record = run()
    record.replay["metadata_logs"] = [
        {
            "kind": "answer",
            "question_id": "current",
            "pass": "fast",
            "model": "test",
            "input_tokens": 100,
            "cached_tokens": 0,
            "output_tokens": 20,
            "cost_usd": 0.01,
        },
        {
            "kind": "pass_cancelled",
            "question_id": "cancelled",
            "model_started": True,
            "input_tokens_est": 40,
            "output_tokens_est": 4,
            "cost_usd_est": 0.005,
        },
        {"kind": "answer", "question_id": "other", "input_tokens": 999, "cost_usd": 9},
    ]
    usage = question_usage(record, record.rows[0])
    assert usage["input_tokens"] == 100
    assert usage["input_tokens_est"] == 40
    assert usage["cost_usd"] == 0.01
    assert usage["cost_usd_est"] == 0.005
    assert usage["input_tokens_missing_records"] == 1
    assert usage["cancelled_records"] == 1


def test_summary_explicitly_marks_missing_semantics_and_useful_timing():
    result = summarize(run())["groups"]["technical"]
    assert result["latency_ms"]["first_useful_sentence_ms"]["n"] == 0
    assert result["latency_ms"]["first_useful_sentence_ms"]["missing"] == 1
    assert result["detailed_assessments"]["redundant"]["percent_of_reviewed"] is None


def test_offline_loader_does_not_require_paid_judging_or_expose_suggestions(tmp_path):
    record = run()
    record.replay["events"].insert(
        0,
        {
            "at": 1,
            "message": {
                "type": "question.detected",
                "question_id": "current",
                "text": "Explain an index.",
                "question_type": "technical",
            },
        },
    )
    save(tmp_path / "replay.json", record.replay)
    save(
        tmp_path / "turns.json",
        [
            {
                "id": 1,
                "question": "Explain an index.",
                "suggestion": "Never send this historical answer to a candidate",
            }
        ],
    )
    loaded = load_run("A", tmp_path)
    assert loaded.rows[0]["fast"] == record.rows[0]["fast"]
    assert not (tmp_path / "answers.json").exists()
    assert loaded.rows[0]["selected_card"] == "current"


def test_quality_scores_remain_missing_until_reviewed_and_reject_invalid_values():
    records = [run(), run("B")]
    review, key = blind_pairs(records, 10, 11)
    side = "A"
    assignment = key["pairs"][review["items"][0]["pair_id"]]["assignment"][side]
    review["items"][0][side]["scores"]["grounding"] = 4
    judgments = read_judgments(review, key)
    record = next(item for item in records if item.architecture == assignment["architecture"])
    quality = summarize(record, judgments[record.architecture])["groups"]["all"]["quality"]
    assert quality["grounding"] == {"n": 1, "missing": 0, "mean": 4}
    assert quality["relevance"]["mean"] is None
    review["items"][0][side]["scores"]["grounding"] = True
    with pytest.raises(ValueError, match="integers"):
        read_judgments(review, key)


def test_out_of_order_ranges_repair_done_but_never_cancelled_answers():
    record = run()

    def event(at, kind, **fields):
        return {
            "at": at,
            "message": {"type": kind, "question_id": "current", "pass": "fast", **fields},
        }

    record.replay["events"] = [
        event(1.1, "answer.delta", text="Second sentence. ", sequence=1),
        event(1.2, "answer.done", delta_count=2),
        event(1.4, "answer.delta", text="First sentence. ", sequence=0),
        event(1.5, "answer.delta", text="Duplicate. ", sequence=0),
    ]
    units = sentence_events(record, record.rows[0])
    assert [unit["text"] for unit in units] == ["First sentence.", "Second sentence."]
    assert [unit["ms"] for unit in units] == pytest.approx([400, 400])
    record.replay["events"].insert(2, event(1.3, "answer.error", code="cancelled"))
    assert sentence_events(record, record.rows[0]) == []


def test_decimal_abbreviation_and_bullet_boundaries_match_production():
    record = run()

    def event(at, text, sequence):
        return {
            "at": at,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": text,
                "sequence": sequence,
            },
        }

    record.replay["events"] = [
        event(1.1, "Dr. Rivera used 3.", 0),
        event(1.2, "5 GB. ", 1),
        event(1.4, "- Reduced query load\n", 2),
    ]
    assert [unit["text"] for unit in sentence_events(record, record.rows[0])] == [
        "Dr. Rivera used 3.5 GB.",
        "- Reduced query load",
    ]


def test_compacted_chunk_ranges_ignore_replayed_interior_chunks():
    record = run()
    record.replay["events"] = [
        {
            "at": 1.1,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "Snapshot answer. ",
                "sequence": 0,
                "sequence_end": 3,
            },
        },
        {
            "at": 1.2,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "Old interior. ",
                "sequence": 1,
            },
        },
    ]
    assert [unit["text"] for unit in sentence_events(record, record.rows[0])] == [
        "Snapshot answer."
    ]


def test_baseline_pair_sample_uses_same_balanced_questions_for_every_candidate():
    records = [run(label) for label in ("A", "B", "C", "D")]
    for record in records:
        record.rows = [
            {**deepcopy(record.rows[0]), "id": index, "category": category}
            for index, category in enumerate(["simple", "technical", "simple", "behavioral"])
        ]
    review, key = blind_pairs(records, 3, 17, baseline="A")
    assert len(review["items"]) == 9
    selected = {}
    for pair in key["pairs"].values():
        labels = {item["architecture"] for item in pair["assignment"].values()}
        assert "A" in labels
        candidate = (labels - {"A"}).pop()
        selected.setdefault(candidate, set()).add(pair["assignment"]["A"]["id"])
    assert selected["B"] == selected["C"] == selected["D"]
    assert review["reviewer_kind"] is None


def test_paired_complete_cases_match_questions_and_preserve_signed_differences():
    from copilot_evals.live_answer_eval import paired_complete_cases

    left, right = run(), run("B")
    right.rows[0]["deep"] = ""
    right.rows[0]["latency"] = {"fast_first_s": 0.1, "fast_done_s": 1.0}
    # A failed second question must not make either architecture's successful mean look better.
    for record in (left, right):
        missing = {**deepcopy(record.rows[0]), "id": 2, "latency": {}}
        record.rows.append(missing)
    result = paired_complete_cases(left, right)["groups"]["all"]
    assert result["completed_pairs"] == 1
    assert result["excluded_incomplete_pairs"] == 1
    assert result["latency_ms"]["complete_ms"]["candidate_minus_baseline"]["p50"] == -500
    assert result["cost"]["fully_reconciled_pairs"] == 0
    assert result["cost"]["baseline"]["observed_usage_priced_usd"] is None
    assert result["latency_ms"]["first_useful_sentence_ms"]["paired_observed"] == 0


def test_useful_fragment_is_not_a_complete_sentence_and_expansion_can_be_first():
    record = run()
    record.replay["events"] = [
        {
            "at": 1.2,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "- Four squads\n- Platform ownership",
            },
        },
        {"at": 1.5, "message": {"type": "answer.done", "question_id": "current", "pass": "fast"}},
        {
            "at": 2.0,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "deep",
                "text": "I lead four squads. ",
            },
        },
        {"at": 2.2, "message": {"type": "answer.done", "question_id": "current", "pass": "deep"}},
    ]
    units = boundary_units(record, record.rows[0])
    assert [(u["id"], u["text"]) for u in units] == [
        ("fast:1", "- Four squads"),
        ("fast:2", "- Platform ownership"),
        ("deep:1", "I lead four squads."),
    ]
    assert units[1]["ms"] == pytest.approx(500)
    assert (
        metrics(record, record.rows[0], {"first_useful_sentence": 1})[
            "first_useful_complete_sentence_ms"
        ]
        is None
    )
    judgment: dict[str, Any] = {
        "first_useful_cue": "fast:1",
        "first_useful_complete_sentence": "deep:1",
    }
    assert metrics(record, record.rows[0], judgment)["first_useful_complete_sentence_ms"] is None
    judgment["unit_grammar"] = {"fast:1": False, "deep:1": True}
    result = metrics(record, record.rows[0], judgment)
    assert result["first_useful_cue_ms"] == pytest.approx(200)
    assert result["first_useful_complete_sentence_ms"] == pytest.approx(1000)


def test_strict_review_requires_explicit_complete_grammar_and_preserves_old_sources():
    review, key = blind_pairs([run(), run("B")], 1, 4)
    side = review["items"][0]["A"]
    side["first_useful_cue"] = "fast:1"
    side["first_useful_complete_sentence"] = "fast:2"
    with pytest.raises(ValueError, match="explicit grammatical"):
        read_judgments(review, key)
    side["unit_grammar"] = {"fast:1": False, "fast:2": True}
    assert read_judgments(review, key)
    side["first_useful_cue"] = "deep:999"
    with pytest.raises(ValueError, match="observed boundary"):
        read_judgments(review, key)


def test_unpunctuated_tail_waits_for_done_and_cancelled_tail_is_not_invented():
    record = run()
    record.replay["events"] = [
        {
            "at": 1.2,
            "message": {
                "type": "answer.delta",
                "question_id": "current",
                "pass": "fast",
                "text": "An incomplete tail",
            },
        },
        {"at": 1.4, "message": {"type": "question.cancelled", "question_id": "current"}},
        {"at": 1.5, "message": {"type": "answer.done", "question_id": "current", "pass": "fast"}},
    ]
    assert boundary_units(record, record.rows[0]) == []


def test_old_modern_model_cost_is_partial_and_old_reservation_is_not_a_verified_bound():
    result = ledger_summary(
        [
            {
                "model": "gpt-6-sol",
                "status": "usage",
                "charged_usd": 0.02,
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
            {"model": "gpt-6-sol", "status": "reserved", "charged_usd": 0.1},
            {
                "model": "gpt-6-sol",
                "status": "usage_unpriced",
                "charged_usd": 0.2,
                "pricing_version": "cache-writes-v1",
                "usage": {"input_tokens": 100},
            },
        ]
    )
    assert result["provider_usage_calls"] == 2
    assert result["provider_usage_priced_calls"] == 0
    assert result["provider_usage_unpriced_calls"] == 2
    assert result["provider_usage_priced_usd"] == 0
    assert result["recorded_usage_price_unverified_usd"] == 0.02
    assert result["unresolved_reserved_upper_usd"] is None
    assert result["verified_reserved_upper_subtotal_usd"] == 0.2
    assert result["unverified_reservation_calls"] == 1


def test_useful_primary_survives_expert_failure_in_started_and_available_pair_scopes():
    left, right = run("A"), run("D")
    left.rows[0]["deep"] = ""
    left.rows[0]["latency"].pop("deep_done_s")
    left.rows[0]["errors"] = [{"stage": "deep", "code": "timeout"}]
    left.rows[0]["failure"] = "RuntimeError"
    judgments = {
        label: {
            "1:0": {
                "first_useful_cue": "fast:1",
                "first_useful_complete_sentence": "fast:1",
                "unit_grammar": {"fast:1": True},
            }
        }
        for label in ("A", "D")
    }
    full = paired_complete_cases(left, right, judgments)
    assert full["groups"]["all"]["completed_pairs"] == 0
    for scope, result in paired_stream_cases(left, right, judgments).items():
        group = result["groups"]["all"]
        assert result["scope"] == scope
        assert group["included_pairs"] == 1
        assert group["latency_ms"]["first_useful_complete_sentence_ms"]["paired_observed"] == 1
        assert group["latency_ms"]["complete_ms"]["paired_observed"] == 0


def test_late_error_excludes_full_completion_even_when_done_timestamps_exist():
    left, right = run("A"), run("D")
    left.rows[0]["errors"] = [{"stage": "deep", "code": "late_failure"}]
    assert paired_complete_cases(left, right)["groups"]["all"]["completed_pairs"] == 0
    assert summarize(left)["groups"]["all"]["success_rate_started"]["successes"] == 0
    assert paired_stream_cases(left, right)["all_started"]["groups"]["all"]["included_pairs"] == 1


def repeated_review_run(label):
    record = run(label)
    original = record.rows[0]
    record.rows = [
        {
            **deepcopy(original),
            "id": f"case-{case}:r{rep}",
            "original_id": f"case-{case}",
            "rep": rep,
            "question": f"Question {case}?",
            "category": "rare" if case == 0 else "common",
        }
        for case in range(12)
        for rep in range(4)
    ]
    record.replay = {"sent": [], "events": [], "speed": 1}
    return record


def selected_review_rows(records, key):
    lookup = {
        record.architecture: {str(row["id"]) + ":" + str(row["rep"]): row for row in record.rows}
        for record in records
    }
    selected = {}
    for pair in key["pairs"].values():
        for assignment in pair["assignment"].values():
            label = assignment["architecture"]
            row = lookup[label][assignment["id"]]
            selected.setdefault((label, row["original_id"]), set()).add(row["rep"])
    return selected


def test_one_per_question_uses_same_outcome_blind_repetition_across_variants():
    records = [repeated_review_run(label) for label in ("A", "B", "D")]
    review, key = blind_pairs(records, 118, 20261125, "A", one_per_question=True)
    assert len(review["items"]) == 24  # Twelve unique questions for each comparison.
    assert review["selection"]["mode"] == "one_per_question"
    selected = selected_review_rows(records, key)
    for case in range(12):
        choices = [selected[(label, f"case-{case}")] for label in ("A", "B", "D")]
        assert choices[0] == choices[1] == choices[2]
        assert len(choices[0]) == 1
    for record in records:
        record.rows.reverse()
        for row in record.rows:
            row.update(fast="", deep="", failure="provider_failure", status="failed", latency={})
    failed_review, failed_key = blind_pairs(records, 118, 20261125, "A", one_per_question=True)
    assert selected_review_rows(records, failed_key) == selected
    assert all(
        item[side]["delivery"]["failure"] for item in failed_review["items"] for side in ("A", "B")
    )
    again, again_key = blind_pairs(records, 118, 20261125, "A", one_per_question=True)
    assert (again, again_key) == (failed_review, failed_key)


def test_one_per_question_and_fixed_repetition_are_mutually_exclusive(tmp_path):
    records = [repeated_review_run(label) for label in ("A", "D")]
    fixed, key = blind_pairs(records, 118, 7, "A", repetition=2)
    assert len(fixed["items"]) == 12
    assert all(reps == {2} for reps in selected_review_rows(records, key).values())
    with pytest.raises(ValueError, match="not both"):
        blind_pairs(records, 118, 7, "A", repetition=2, one_per_question=True)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--trial-run",
                "unused",
                "--out",
                str(tmp_path),
                "--review-rep",
                "0",
                "--review-one-per-question",
            ]
        )
    assert error.value.code == 2


def test_scoped_analysis_preserves_exact_summaries_pairs_and_blind_outputs():
    import copilot_evals.live_answer_eval as evaluation

    left, right = run("A"), run("D")
    for record in (left, right):
        record.replay["metadata_logs"] = [
            {
                "kind": "answer",
                "question_id": "current",
                "pass": "fast",
                "input_tokens": 17,
                "output_tokens": 4,
                "cost_usd": 0.01,
                "latency_ms": {"model_dispatch_ms": 7, "router_first_chunk_ms": 90},
            },
            {"kind": "pass_cancelled", "question_id": "cancelled", "input_tokens_est": 20},
        ]
        record.replay["events"].extend(
            [
                {
                    "at": 1.6,
                    "message": {
                        "type": "answer.delta",
                        "question_id": "current",
                        "pass": "deep",
                        "text": "A complete explanation. Final tail",
                        "sequence": 0,
                    },
                },
                {
                    "at": 2.5,
                    "message": {
                        "type": "answer.done",
                        "question_id": "current",
                        "pass": "deep",
                        "delta_count": 1,
                    },
                },
            ]
        )
    judgments = {
        "A": {"1:0": {"first_useful_cue": "fast:1"}},
        "D": {
            "1:0": {"first_useful_complete_sentence": "deep:1", "unit_grammar": {"deep:1": True}}
        },
    }
    expected = {
        "summary": evaluation._summarize(left, judgments["A"]),
        "pairs": {
            scope: evaluation._paired_complete_cases(left, right, judgments, scope=scope)
            for scope in ("full_answer_complete", "all_started", "primary_available")
        },
        "review": evaluation._blind_pairs([left, right], 1, 42),
    }
    with evaluation.analysis_scope(left, right):
        actual = {
            "summary": summarize(left, judgments["A"]),
            "pairs": {
                scope: paired_complete_cases(left, right, judgments, scope=scope)
                for scope in ("full_answer_complete", "all_started", "primary_available")
            },
            "review": blind_pairs([left, right], 1, 42),
        }
    assert actual == expected


def test_analysis_indexes_once_and_scoped_cache_isolates_results_judgments_and_invocations(
    monkeypatch,
):
    import copilot_evals.live_answer_eval as evaluation

    class CountedEvents(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    record = run()
    events = CountedEvents(record.replay["events"])
    record.replay["events"] = events
    parsed = []
    original = evaluation._sentence_events

    def count(*args, **kwargs):
        parsed.append((args[2], kwargs.get("terminal_tail")))
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, "_sentence_events", count)
    judgment = {"first_useful_sentence": 1}
    with evaluation.analysis_scope(record):
        first = metrics(record, record.rows[0], judgment)
        first["first_useful_sentence_ms"] = -99
        assert metrics(record, record.rows[0], judgment)[
            "first_useful_sentence_ms"
        ] == pytest.approx(400)
        units = boundary_units(record, record.rows[0])
        units[0]["text"] = "Caller mutation"
        assert boundary_units(record, record.rows[0])[0]["text"] != "Caller mutation"
        judgment["first_useful_sentence"] = 2
        assert metrics(record, record.rows[0], judgment)[
            "first_useful_sentence_ms"
        ] == pytest.approx(800)
        summarize(record)
        with evaluation.analysis_scope(record):
            summarize(record)
        assert parsed == [("fast", False), ("fast", True), ("deep", True)]
        assert events.iterations == 1
    # Low-level calls outside a scope and later invocations must see source changes.
    record.replay["events"][2]["at"] = 1.5
    assert metrics(record, record.rows[0], {"first_useful_sentence": 1})[
        "first_useful_sentence_ms"
    ] == pytest.approx(500)
    with evaluation.analysis_scope(record):
        assert metrics(record, record.rows[0], {"first_useful_sentence": 1})[
            "first_useful_sentence_ms"
        ] == pytest.approx(500)
    assert events.iterations == 5  # One index, three direct parsers, one fresh index.


def test_scoped_bootstrap_reuse_preserves_rng_statistics_and_defensive_copy(monkeypatch):
    import copilot_evals.live_answer_stats as stats

    observations = [("one", -2.0), ("one", 3.0), ("two", 1.0), ("three", 7.0)]
    expected = stats.paired_uncertainty(observations, seed=21, replicates=50)
    calls = []
    original = stats._paired_uncertainty

    def count(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(stats, "_paired_uncertainty", count)
    with stats.bootstrap_scope():
        first = stats.paired_uncertainty(observations, seed=21, replicates=50)
        assert first == expected
        first["mean_difference_95"][0] = -100
        with stats.bootstrap_scope():
            assert stats.paired_uncertainty(observations, seed=21, replicates=50) == expected
        assert len(calls) == 1
        stats.paired_uncertainty(observations, seed=22, replicates=50)
        assert len(calls) == 2
    with stats.bootstrap_scope():
        assert stats.paired_uncertainty(observations, seed=21, replicates=50) == expected
    assert len(calls) == 3
