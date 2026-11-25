"""Controlled generation replay: offline protocol validation and leakage/cost guards."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from copilot_evals.live_answer_eval import comparable, metrics
from copilot_evals.live_answer_replay import (
    STRATEGIES,
    TRIAL,
    AuditBudget,
    BudgetBlocked,
    load_cases,
    normalized_runs,
    preflight,
    validate_cases,
)

from copilot_core.pricing import ModelPrice


def test_frozen_cases_include_scope_and_exclude_historical_answer_fields():
    cases = load_cases()
    assert len(cases) == 51
    assert {case["category"] for case in cases} >= {
        "simple",
        "behavioral",
        "technical",
        "system_design",
        "standup",
    }
    assert all(case["source"] and case["context_sha256"] for case in cases)
    assert not any({"suggestion", "fast", "deep", "answer"} & case.keys() for case in cases)
    contaminated = deepcopy(cases[:1])
    contaminated[0]["suggestion"] = "Previous model output"
    with pytest.raises(ValueError, match="leakage"):
        validate_cases(contaminated)
    changed = deepcopy(cases[:1])
    changed[0]["digest"] = "A different profile"
    with pytest.raises(ValueError, match="fingerprint"):
        validate_cases(changed)


async def test_offline_preflight_uses_all_strategies_and_never_constructs_real_providers(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline preflight attempted to construct a real provider")

    monkeypatch.setattr("copilot_evals.live_answer_replay.make_providers", forbidden)
    await preflight(load_cases())


def test_budget_records_trial_and_retains_unknown_usage_reservation(tmp_path):
    settings = SimpleNamespace(
        model_prices={"offline-model": ModelPrice(input=1, cached=0.1, output=2)},
        tier_price_multipliers={"standard": 1},
    )
    budget = AuditBudget(tmp_path, settings, limit=0.1)
    token = TRIAL.set("case:adaptive:0")
    try:
        ticket = budget.reserve("offline-model", "question", 10)
        assert budget.entries[ticket]["trial_id"] == "case:adaptive:0"
        assert budget.entries[ticket]["status"] == "reserved"
        reserved = budget.total
        with pytest.raises(BudgetBlocked):
            budget.reserve("offline-model", "question", 1_000_000)
        assert budget.total == reserved
        assert budget.blocked
        assert budget.blocked_trials == {"case:adaptive:0"}
    finally:
        TRIAL.reset(token)


def recorded_trial(case, strategy):
    qid = strategy + "-question"
    return {
        "id": case["id"],
        "architecture": strategy,
        "rep": 0,
        "question": case["question"],
        "context_sha256": case["context_sha256"],
        "failure": None,
        "events": [
            {
                "at": 0.2,
                "message": {
                    "type": "answer.delta",
                    "question_id": qid,
                    "pass": "fast",
                    "text": "A direct answer. ",
                    "sequence": 0,
                },
            },
            {
                "at": 0.5,
                "message": {
                    "type": "answer.done",
                    "question_id": qid,
                    "pass": "fast",
                    "delta_count": 1,
                },
            },
        ],
        "metadata_logs": [
            {
                "kind": "answer",
                "question_id": qid,
                "pass": "fast",
                "input_tokens": 100,
                "output_tokens": 10,
                "cost_usd": 0.001,
            }
        ],
    }


def test_generation_adapter_preserves_repetition_and_source_without_mutating_trials():
    cases = load_cases()[:1]
    trials = [recorded_trial(cases[0], strategy) for strategy in STRATEGIES.values()]
    before = deepcopy(trials)
    runs = normalized_runs(trials, cases, {"code": "fixed"}, [])
    assert trials == before
    assert len(runs) == 4
    assert all(comparable(runs[0], other)["comparable"] for other in runs[1:])
    for run in runs:
        row = run.rows[0]
        assert row["original_id"] == cases[0]["id"]
        assert row["rep"] == 0
        assert row["fast"] == "A direct answer. "
        assert row["evidence"]["digest"] == cases[0]["digest"]
        assert metrics(run, row, {"first_useful_sentence": 1})["first_useful_sentence_ms"] == 200
        assert run.replay["generation_only"]
    trials[0]["context_sha256"] = "changed"
    with pytest.raises(ValueError, match="context"):
        normalized_runs(trials, cases, {}, [])


async def test_budget_rejection_stops_dispatch_and_is_not_provider_failure(tmp_path, monkeypatch):
    import argparse
    import json

    import copilot_evals.live_answer_replay as replay

    from copilot_gateway.config import Settings
    from copilot_gateway.testing import ScriptedProvider

    settings_type = Settings

    def settings(**values):
        defaults = {
            "_env_file": None,
            "llm_providers": ["openai"],
            "openai_detector_model": "offline-model",
            "openai_fast_model": "offline-model",
            "openai_deep_model": "offline-model",
            "openai_summary_model": "offline-model",
            "openai_detector_reasoning": "none",
            "openai_fast_reasoning": "none",
            "openai_deep_reasoning": "low",
            "openai_summary_reasoning": "none",
            "model_prices": {"offline-model": ModelPrice(input=1, cached=0.1, output=2)},
        }
        defaults.update(values)
        return settings_type(**defaults)

    provider = ScriptedProvider("openai")
    monkeypatch.setattr(replay, "Settings", settings)
    monkeypatch.setattr(replay, "make_providers", lambda *a, **k: [provider])
    monkeypatch.setattr(replay, "experiment_identity", lambda *a: {"test": "frozen"})
    source = tmp_path / "input.json"
    source.write_text(json.dumps(load_cases()[:1]), encoding="utf-8")
    out = tmp_path / "run"
    args = argparse.Namespace(
        cases=source,
        out=out,
        live=True,
        reps=1,
        seed=3,
        concurrency=2,
        max_cost=0.00000001,
        timeout=10,
    )
    await replay.execute(args)
    trials = json.loads((out / "trials.json").read_text("utf-8"))
    accounting = json.loads((out / "accounting.json").read_text("utf-8"))
    assert provider.calls == []
    assert accounting["failed_trials"] == 0
    assert accounting["budget_blocked_trials"] >= 1
    assert accounting["not_started"] >= 1
    assert all(row["status"] == "budget_blocked" for row in trials)
    assert (
        len(json.loads((out / "not-started.json").read_text("utf-8"))) == accounting["not_started"]
    )


async def test_router_close_after_done_preserves_completed_request_and_actual_usage(tmp_path):
    from copilot_evals.live_answer_replay import AuditProvider

    from copilot_core.llm import LLMDone, LLMRequest
    from copilot_gateway.testing import Reply, ScriptedProvider

    settings = SimpleNamespace(
        model_prices={"offline-model": ModelPrice(input=1, cached=0.1, output=2)},
        tier_price_multipliers={"standard": 1},
    )
    budget = AuditBudget(tmp_path, settings, limit=1)
    records = []
    provider = AuditProvider(
        ScriptedProvider("offline", lambda *a: Reply(["Answer."])), budget, records, tmp_path
    )
    stream = provider.stream("offline-model", LLMRequest([], "question", 50), {})
    assert await anext(stream) == "Answer."
    assert isinstance(await anext(stream), LLMDone)
    await stream.aclose()
    assert records[0]["status"] == "completed"
    assert records[0]["elapsed_s"] >= 0
    assert budget.entries[0]["status"] == "usage"


def test_normalization_rejects_duplicate_or_unknown_architecture_trials():
    cases = load_cases()[:1]
    item = recorded_trial(cases[0], "adaptive")
    with pytest.raises(ValueError, match="Duplicate"):
        normalized_runs([item, item], cases, {}, [])
    item["architecture"] = "unknown"
    with pytest.raises(ValueError, match="Unknown"):
        normalized_runs([item], cases, {}, [])


def test_trial_costs_include_hedge_reservations_but_are_not_invented_actual_cost():
    from copilot_evals.live_answer_eval import question_usage

    cases = load_cases()[:1]
    trials = [recorded_trial(cases[0], strategy) for strategy in STRATEGIES.values()]
    ledger = [
        {
            "trial_id": f"{cases[0]['id']}:adaptive:0",
            "status": "usage",
            "model": "offline-model",
            "charged_usd": 0.01,
        },
        {
            "trial_id": f"{cases[0]['id']}:adaptive:0",
            "status": "reserved",
            "model": "offline-model",
            "charged_usd": 0.1,
        },
        {
            "trial_id": f"{cases[0]['id']}:legacy_dual:0",
            "status": "usage",
            "model": "offline-model",
            "charged_usd": 0.03,
        },
    ]
    records = normalized_runs(trials, cases, {}, ledger)
    adaptive = records[1]
    assert len(adaptive.ledger) == 2
    costs = question_usage(adaptive, adaptive.rows[0])["all_request_ledger"]
    assert costs["provider_usage_priced_usd"] == 0.01
    assert costs["unresolved_reserved_upper_usd"] == 0.1
