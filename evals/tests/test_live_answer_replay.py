"""Controlled generation replay: offline protocol validation and leakage/cost guards."""

from argparse import Namespace
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

from copilot_core.llm import LLMDone, LLMUsage
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
    monkeypatch.setattr(replay, "experiment_identity", lambda *a: {"test": "frozen", "code": {}})
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
        skip_connectivity_check=True,
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


def test_explicit_no_currency_cutoff_keeps_finite_default_and_rejects_invalid_values():
    import argparse

    from copilot_evals.live_answer_replay import budget_limit, settings_overrides

    assert budget_limit("none") is None
    assert budget_limit("2.50") == 2.5
    for value in ("inf", "nan", "-1", "0"):
        with pytest.raises(argparse.ArgumentTypeError):
            budget_limit(value)
    assert settings_overrides(Namespace(fast_model="configured-model", expert_reasoning="low")) == {
        "openai_fast_model": "configured-model",
        "openai_deep_reasoning": "low",
    }


def test_tier_override_changes_both_attempts_without_adding_routes():
    from copilot_evals.live_answer_replay import tier_overrides

    from copilot_core.llm import ModelRouter, Route
    from copilot_gateway.testing import ScriptedProvider

    provider = ScriptedProvider("openai")
    router = ModelRouter(
        {
            job: [Route(provider, "model"), Route(provider, "model")]
            for job in ("fast", "deep_light", "deep")
        },
        3,
    )
    tier_overrides(router, SimpleNamespace(fast_tier="priority", expert_tier="default"))
    assert len(router.routes["fast"]) == 2
    assert all(route.params["service_tier"] == "priority" for route in router.routes["fast"])
    assert all("service_tier" not in route.params for route in router.routes["deep"])


def test_unknown_cache_write_usage_retains_reservation_and_reasoning_is_not_double_counted(
    tmp_path,
):
    settings = SimpleNamespace(
        model_prices={"gpt-6-sol": ModelPrice(input=2, cached=0.2, output=10)},
        tier_price_multipliers={"standard": 1},
    )
    budget = AuditBudget(tmp_path, settings, 1)
    ticket = budget.reserve("gpt-6-sol", "payload", 50)
    upper = budget.entries[ticket]["charged_usd"]
    assert budget.entries[ticket]["reservation_assumption"] == "all input is 12h cache writes"
    budget.finish(
        ticket,
        LLMDone(
            LLMUsage(input_tokens=100, output_tokens=30, reasoning_tokens=20),
            "stop",
            "openai",
            "gpt-6-sol",
        ),
    )
    assert budget.entries[ticket]["status"] == "usage_unpriced"
    assert budget.entries[ticket]["charged_usd"] == upper
    assert budget.entries[ticket]["usage"]["reasoning_tokens"] == 20
    budget.finish(
        ticket,
        LLMDone(
            LLMUsage(input_tokens=100, output_tokens=30, cache_write_tokens=0, reasoning_tokens=20),
            "stop",
            "openai",
            "gpt-6-sol",
        ),
    )
    assert budget.entries[ticket]["status"] == "usage"
    assert budget.entries[ticket]["charged_usd"] == pytest.approx(0.0005)


async def test_live_entry_holds_exclusive_lock_but_offline_preflight_does_not(
    tmp_path, monkeypatch
):
    from copilot_evals import live_answer_replay as harness
    from copilot_evals.live_answer_artifacts import run_lock

    target = tmp_path / "locked-run"
    target.mkdir()
    checked = []

    async def offline(cases, variants):
        with run_lock(target):
            checked.append("offline-unlocked")

    async def live(args, cases, settings, rate_identity, variants, directory):
        assert directory == target
        with pytest.raises(ValueError, match="Another runner"), run_lock(directory):
            pytest.fail("Concurrent paid resume was allowed")
        checked.append("paid-locked")

    monkeypatch.setattr(harness, "preflight", offline)
    monkeypatch.setattr(harness, "execute_live", live)
    args = Namespace(
        cases=None, live=True, out=target, reps=1, concurrency=1, max_cost=None, timeout=10
    )
    await harness.execute(args)
    assert checked == ["offline-unlocked", "paid-locked"]
    with run_lock(target):
        pass
    args.live = False
    await harness.execute(args)
    assert checked == ["offline-unlocked", "paid-locked", "offline-unlocked"]


def test_independent_repetitions_use_trial_identity_not_prior_matching_text():
    cases = load_cases()[:1]
    trials = []
    for rep in range(4):
        trial = recorded_trial(cases[0], "legacy_dual")
        trial["rep"] = rep
        trial["question_id"] = f"question-{rep}"
        for event in trial["events"]:
            event["message"]["question_id"] = trial["question_id"]
            event["at"] += rep / 10
        trial["events"][0]["message"]["text"] = f"Repetition {rep}. "
        trial["metadata_logs"][0]["question_id"] = trial["question_id"]
        trials.append(trial)
    record = normalized_runs(trials, cases, {"strategies": {"A": "legacy_dual"}}, [])[0]
    assert len(record.rows) == 4
    for rep, row in enumerate(record.rows):
        assert row["attribution_ambiguities"] == []
        assert row["selected_card"] == f"question-{rep}"
        assert row["fast"] == f"Repetition {rep}. "
        assert row["latency"]["fast_done_s"] == pytest.approx(0.5 + rep / 10)
        assert row["card_audit"][0]["attribution"][0]["candidate_turn_ids"] == [row["id"]]
    trials[1]["question_id"] = trials[0]["question_id"]
    with pytest.raises(ValueError, match="Question ID reused"):
        normalized_runs(trials, cases, {"strategies": {"A": "legacy_dual"}}, [])


def admission_budget(path):
    return AuditBudget(
        path,
        SimpleNamespace(
            model_prices={"offline-model": ModelPrice(input=1, cached=0.1, output=2)},
            tier_price_multipliers={"standard": 1},
        ),
        limit=1,
    )


async def test_shared_provider_gate_bounds_streams_and_cancels_queue_before_reserving(tmp_path):
    import asyncio

    from copilot_evals.live_answer_replay import AuditProvider, ProviderAdmission

    from copilot_core.llm import LLMRequest

    class HeldProvider:
        name = "offline"
        active = 0
        peak = 0
        closed = 0

        async def stream(self, *args):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                yield "Answer."
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.active -= 1
                self.closed += 1

    raw, gate, records = HeldProvider(), ProviderAdmission(2), []
    budget = admission_budget(tmp_path)
    # Separate wrappers still share one experiment-wide gate.
    providers = [AuditProvider(raw, budget, records, tmp_path, admission=gate) for _ in range(2)]
    request = LLMRequest([], "question", 50)
    streams = [providers[i % 2].stream("offline-model", request, {}) for i in range(3)]
    assert await anext(streams[0]) == "Answer."
    assert await anext(streams[1]) == "Answer."
    queued = asyncio.create_task(anext(streams[2]))
    await asyncio.sleep(0)
    assert gate.active == raw.active == gate.peak == 2
    assert gate.waiting == 1
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(budget.entries) == 2
    assert records[-1]["dispatch_admitted"] is False
    assert records[-1]["admission_wait_ms"] >= 0
    await streams[0].aclose()
    assert raw.closed == 1
    assert gate.active == raw.active == 1
    await streams[1].aclose()
    assert gate.active == raw.active == gate.waiting == 0
    assert raw.peak == 2


async def test_rate_limit_stops_queued_dispatch_and_keeps_reservations_distinct(tmp_path):
    import asyncio

    from copilot_evals.live_answer_replay import AdmissionBlocked, AuditProvider, ProviderAdmission

    from copilot_core.llm import LLMRequest

    release = asyncio.Event()

    class RateLimitError(Exception):
        status_code = 429

    class LimitedProvider:
        name = "offline"
        calls = 0

        async def stream(self, *args):
            self.calls += 1
            await release.wait()
            raise RateLimitError("bounded synthetic 429")
            yield "unreachable"

    raw, gate, records = LimitedProvider(), ProviderAdmission(1), []
    budget = admission_budget(tmp_path)
    provider = AuditProvider(raw, budget, records, tmp_path, admission=gate)
    request = LLMRequest([], "question", 50)
    first = asyncio.create_task(anext(provider.stream("offline-model", request, {})))
    await asyncio.sleep(0)
    second = asyncio.create_task(anext(provider.stream("offline-model", request, {})))
    await asyncio.sleep(0)
    assert gate.waiting == 1
    release.set()
    with pytest.raises(RateLimitError):
        await first
    with pytest.raises(AdmissionBlocked, match="rate_limit"):
        await second
    with pytest.raises(AdmissionBlocked, match="rate_limit"):
        await anext(provider.stream("offline-model", request, {}))
    assert raw.calls == len(budget.entries) == 1
    assert budget.entries[0]["status"] == "reserved"  # Provider returned no usage.
    assert gate.active == gate.waiting == 0
    assert gate.snapshot()["blocked_before_reservation"] == 2
    assert records[1]["admission_blocked_reason"] == "rate_limit"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (400, "request_configuration"),
        (422, "request_configuration"),
        (429, "rate_limit"),
        (408, None),
        (503, None),
    ],
)
def test_gate_immediate_fatal_circuit_is_distinct_from_transient_retry_policy(status, reason):
    from copilot_evals.live_answer_replay import ProviderAdmission

    class StatusError(Exception):
        status_code = status

    stopped = []
    gate = ProviderAdmission(8, stopped.append)
    gate.observe_error(StatusError())
    assert gate.reason == reason
    assert stopped == ([reason] if reason else [])


async def test_hedged_attempt_waits_for_actual_cancelled_provider_close(tmp_path):
    import asyncio

    from copilot_evals.live_answer_replay import AuditProvider, ProviderAdmission

    from copilot_core.llm import LLMRequest, ModelRouter, Route

    class SlowFirstProvider:
        name = "offline"
        calls = 0
        active = 0
        peak = 0

        async def stream(self, model, req, params):
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.calls == 1:
                    await asyncio.sleep(1)
                yield "Answer."
                yield LLMDone(LLMUsage(input_tokens=20, output_tokens=2), "stop", self.name, model)
            finally:
                await asyncio.sleep(0)
                self.active -= 1

    raw, gate, records = SlowFirstProvider(), ProviderAdmission(1), []
    budget = admission_budget(tmp_path)
    provider = AuditProvider(raw, budget, records, tmp_path, admission=gate)
    router = ModelRouter(
        {"fast": [Route(provider, "offline-model"), Route(provider, "offline-model")]},
        first_token_timeout_s=0.1,
        hedge_after_s={"fast": 0.05},
    )
    text, done = await asyncio.wait_for(router.complete("fast", LLMRequest([], "question", 20)), 2)
    assert text == "Answer."
    assert done.hedged
    assert raw.calls == 2
    assert raw.peak == gate.peak == 1
    assert raw.active == gate.active == 0
    assert records[1]["admission_wait_ms"] > 0
    assert [entry["status"] for entry in budget.entries] == ["reserved", "usage"]


async def test_eight_bounded_workers_preserve_case_blocks_and_stop_remaining_schedule():
    import asyncio

    from copilot_evals.live_answer_replay import bounded_blocks

    active = peak = 0
    completed = []
    release = asyncio.Event()
    stop = False
    blocks = [[({"id": case}, variant, 0) for variant in ("A", "D")] for case in range(100)]

    async def run_one(case, variant, rep):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        completed.append((case["id"], variant, rep))
        active -= 1

    task = asyncio.create_task(bounded_blocks(blocks, 8, run_one, lambda: stop))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active == peak == 8
    stop = True
    release.set()
    await task
    assert len(completed) == 8  # Inflight drain; no additional condition/case dispatch.
    assert all(variant == "A" for _, variant, _ in completed)
    stop = False
    completed.clear()
    await bounded_blocks(blocks[:10], 8, run_one, lambda: stop)
    assert len(completed) == 20
    for case in range(10):
        assert [v for c, v, _ in completed if c == case] == ["A", "D"]
    with pytest.raises(ValueError, match="eight"):
        await bounded_blocks(blocks, 9, run_one, lambda: False)
