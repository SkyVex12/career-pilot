"""Coverage, counterbalancing, circuit, uncertainty and blind-review safety regressions."""

import json
from collections import Counter, defaultdict
from copy import deepcopy

import pytest
from copilot_evals.live_answer_corpus import representative_cases
from copilot_evals.live_answer_eval import blind_pairs, summarize
from copilot_evals.live_answer_replay import (
    load_cases,
    normalized_runs,
    root_directory,
    validate_cases,
)
from copilot_evals.live_answer_review import review_html
from copilot_evals.live_answer_schedule import FailureCircuit, case_blocks, classify_failure
from copilot_evals.live_answer_stats import paired_uncertainty, rate


def recorded_trial(case, strategy):
    return {
        "id": case["id"],
        "architecture": strategy,
        "rep": 0,
        "question": case["question"],
        "context_sha256": case["context_sha256"],
        "failure": None,
        "status": "completed",
        "metadata_logs": [],
        "events": [
            {
                "at": 0.2,
                "message": {
                    "type": "answer.delta",
                    "question_id": strategy,
                    "pass": "fast",
                    "text": "A useful answer. ",
                    "sequence": 0,
                },
            },
            {
                "at": 0.5,
                "message": {
                    "type": "answer.done",
                    "question_id": strategy,
                    "pass": "fast",
                    "delta_count": 1,
                },
            },
        ],
    }


def run(label):
    strategy = {"A": "legacy_dual", "D": "adaptive_on_demand"}[label]
    cases = load_cases()[:1]
    return normalized_runs(
        [recorded_trial(cases[0], strategy)], cases, {"strategies": {label: strategy}}, []
    )[0]


def test_representative_sources_are_labeled_and_reference_answers_withheld():
    original = load_cases()
    frozen = deepcopy(original)
    cases = validate_cases(representative_cases(original, root_directory()))
    assert original == frozen
    assert len(cases) == 118
    assert len({c["id"] for c in cases}) == 118
    assert sum(c["id"].startswith("knowledge-") for c in cases) == 37
    assert sum(c["id"].startswith("edge-") for c in cases) == 8
    assert all(c["source"] and c["context_sha256"] for c in cases)
    assert not any(
        {"answer", "fast", "deep", "fact", "must", "must_not", "trap", "key_points"} & c.keys()
        for c in cases
    )
    assert any(c["type"] == "system_design" for c in cases)
    assert any(c["category"] == "standup_technical" and c["memory"] for c in cases)


def test_case_blocks_are_deterministic_and_counterbalanced_within_question():
    cases = [{"id": str(i)} for i in range(7)]
    blocks = case_blocks(cases, ["legacy", "candidate"], 2, 41)
    assert blocks == case_blocks(cases, ["legacy", "candidate"], 2, 41)
    assert len(blocks) == 14
    starts = defaultdict(list)
    for block in blocks:
        assert len({case["id"] for case, _, _ in block}) == 1
        assert {strategy for _, strategy, _ in block} == {"legacy", "candidate"}
        starts[block[0][0]["id"]].append(block[0][1])
    assert all(Counter(order) == {"legacy": 1, "candidate": 1} for order in starts.values())


def test_circuit_distinguishes_transport_failures_from_slow_or_recovered_requests():
    failure = {"failure": "llm_error"}
    transport = [
        {"status": "APITimeoutError", "error_types": ["APITimeoutError", "ConnectTimeout"]}
    ]
    assert classify_failure(failure, transport) == "transport"
    circuit = FailureCircuit(threshold=2)
    circuit.observe(failure, transport)
    assert circuit.reason is None
    circuit.observe(failure, [{"status": "CancelledError"}])
    assert circuit.consecutive == 0  # A slow first-token cancellation is not proof of outage.
    circuit.observe(failure, [*transport, {"status": "completed"}])
    assert circuit.consecutive == 0  # An alternate completed, so transport is available.
    circuit.observe(failure, transport)
    circuit.observe(failure, transport)
    assert circuit.reason == "transport"
    auth = FailureCircuit()
    auth.observe(failure, [{"error_types": ["AuthenticationError"]}])
    assert auth.reason == "authentication"


def test_not_started_cases_preserve_planned_denominators_without_fake_provider_failure():
    cases = load_cases()[:2]
    trials = [recorded_trial(cases[0], "adaptive_on_demand")]
    pending = [
        {
            "id": cases[1]["id"],
            "architecture": "adaptive_on_demand",
            "rep": 0,
            "reason": "transport",
        }
    ]
    record = normalized_runs(
        trials, cases, {"strategies": {"D": "adaptive_on_demand"}}, [], pending
    )[0]
    summary = summarize(record)["groups"]["all"]
    assert summary["planned"] == 2
    assert summary["started"] == 1
    assert summary["not_started"] == 1
    assert summary["failures"] == 0
    assert summary["success_rate_started"]["rate"] == 1
    assert summary["completion_coverage_planned"]["rate"] == 0.5
    assert summary["latency_ms"]["complete_ms"]["missing"] == 1
    with pytest.raises(ValueError, match="distinct"):
        normalized_runs(trials, cases, {}, [], [{**pending[0], "id": cases[0]["id"]}])


def test_rates_and_clustered_uncertainty_keep_missingness_and_repetitions_explicit():
    assert rate(0, 0)["rate"] is None
    all_success = rate(10, 10)
    assert all_success["wilson_95"][0] < 1
    assert all_success["wilson_95"][1] == pytest.approx(1)
    with pytest.raises(ValueError, match="denominator"):
        rate(2, 1)
    observations = [("question1", -100), ("question1", -100), ("question2", 200)]
    ci = paired_uncertainty(observations)
    assert ci["observed_pairs"] == 3
    assert ci["unique_questions"] == 2
    assert ci == paired_uncertainty(observations)
    assert ci["mean_difference_95"][0] <= 0 <= ci["mean_difference_95"][1]
    assert paired_uncertainty([("one", 5)])["mean_difference_95"] is None


def test_human_page_escapes_untrusted_source_and_has_no_automatic_human_judgment():
    review, key = blind_pairs([run("A"), run("D")], 1, 3, "A")
    review["items"][0]["question"] = '</script><script>fetch("https://invalid")</script>'
    html = review_html(review)
    assert "</script><script>fetch(" not in html
    encoded = html.split('<script type="application/json" id="source">')[1].split("</script>")[0]
    data = json.loads(encoded)
    assert data == json.loads(json.dumps(review))
    assert data["reviewer_kind"] is None
    assert all(item["preference"] is None for item in data["items"])
    assert "connect-src 'none'" in html
    assert "textContent" in html
    assert "innerHTML" not in html
    assert "if(!byId('attest').checked" in html
    assert json.dumps(key) not in html
    reviewed = deepcopy(review)
    reviewed["reviewer_kind"] = "ai"
    with pytest.raises(ValueError, match="unreviewed"):
        review_html(reviewed)


def test_zero_turn_knowledge_cases_are_synthetic_not_historical():
    cases = representative_cases(load_cases(), root_directory())
    by_id = {case["id"]: case for case in cases}
    for identifier in ("knowledge-retry_count_unknown", "knowledge-sftp_design_unknown"):
        assert by_id[identifier]["source"] == "synthetic missing-context coverage"
    assert sum("historical" in case["source"] for case in cases) == 79
    assert sum("historical" not in case["source"] for case in cases) == 39
