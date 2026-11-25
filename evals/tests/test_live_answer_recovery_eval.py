"""Explicit recovery fault conditions; all provider work here is scripted offline."""

import asyncio
import json
from argparse import Namespace

import httpx2
import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals.live_answer_schedule import classify_failure
from copilot_evals.live_answer_variants import SyntheticFault, Variant, load_variants

from copilot_core.pricing import ModelPrice
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.testing import Reply, ScriptedProvider


def settings(recovery=False):
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] - BaseSettings runtime keyword.
        llm_providers=["openai"],
        answer_architecture="adaptive_on_demand",
        adaptive_failure_recovery=recovery,
        openai_detector_model="offline-fast",
        openai_fast_model="offline-fast",
        openai_deep_model="offline-expert",
        openai_detector_reasoning="none",
        openai_fast_reasoning="none",
        openai_deep_reasoning="low",
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        model_prices={
            "offline-fast": ModelPrice(input=1, cached=0.1, output=2),
            "offline-expert": ModelPrice(input=2, cached=0.2, output=3),
        },
    )


def variant(recovery, fault=None):
    return Variant(
        "E" if recovery else "D",
        "adaptive_on_demand",
        settings(recovery),
        synthetic_fault=fault,
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "after_first_chunk"},
        {"kind": "connect_before_dispatch", "delay_ms": 251},
        {"kind": "connect_before_dispatch", "delay_ms": True},
        {"kind": "connect_before_dispatch", "jobs": ["deep"]},
        {},
    ],
)
def test_manifest_rejects_unsupported_faults_and_unbounded_delays(tmp_path, spec):
    path = tmp_path / "variants.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "variants": [
                    {
                        "label": "E",
                        "architecture": "adaptive_on_demand",
                        "synthetic_fault": spec,
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match=r"fault|Fault|pre-dispatch"):
        load_variants(path, settings(), tmp_path)


def test_fault_manifest_is_explicit_archived_and_does_not_mutate_base(tmp_path):
    path = tmp_path / "variants.json"
    item = {
        "label": "E",
        "architecture": "adaptive_on_demand",
        "settings": {"adaptive_failure_recovery": True},
        "synthetic_fault": {"kind": "connect_before_dispatch", "delay_ms": 5},
    }
    path.write_text(json.dumps({"schema": 1, "variants": [item]}))
    base = settings()
    condition = load_variants(path, base, tmp_path)["E"]
    assert not base.adaptive_failure_recovery
    assert condition.identity(replay.CONFIG_FIELDS)["synthetic_fault"] == item["synthetic_fault"]
    item["architecture"] = "legacy_dual"
    path.write_text(json.dumps({"schema": 1, "variants": [item]}))
    with pytest.raises(ValueError, match="adaptive_on_demand"):
        load_variants(path, base, tmp_path)


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("injected", [False, True])
async def test_fault_control_matrix_uses_zero_primary_dispatch_and_one_metered_recovery(
    tmp_path, recovery, injected
):
    condition = variant(recovery, SyntheticFault() if injected else None)
    case = replay.load_cases()[0]
    raw = ScriptedProvider("openai", lambda *args: Reply(["A direct grounded answer. "]))
    budget = replay.AuditBudget(tmp_path, condition.settings, 1)
    requests, faults = [], {}
    audited = replay.AuditProvider(raw, budget, requests, tmp_path)
    router = build_router(condition.settings, [audited])
    original_recovery = router.routes.get("answer_recovery")
    replay.apply_synthetic_fault(
        router, condition, lambda row: faults.update({row["fault_id"]: row})
    )
    assert router.routes.get("answer_recovery") is original_recovery
    token = replay.TRIAL.set(f"{case['id']}:{condition.label}:0")
    try:
        result = await replay.trial(case, condition.label, 0, condition.settings, router, 5)
    finally:
        replay.TRIAL.reset(token)
    replay.annotate_synthetic_faults(result, condition, list(faults.values()))
    if injected and not recovery:
        assert result["failure"] == "llm_unavailable"
        assert result["expected_injected_failure"]
        assert not requests
        assert not budget.entries
        assert not raw.calls
        assert classify_failure(replay.real_failure_evidence(result), requests) is None
    else:
        assert result["failure"] is None
        assert len(requests) == len(budget.entries) == len(raw.calls) == 1
        assert budget.entries[0]["status"] == "usage"
        assert budget.entries[0]["charged_usd"] > 0
        expected_model = "offline-expert" if injected else "offline-fast"
        assert raw.calls[0][1] == expected_model
    if injected:
        assert faults
        assert all(row["provider_dispatched"] is False for row in faults.values())
        assert all(row["api_cost_usd"] == 0 for row in faults.values())
        assert all(row["status"] == "injected" for row in faults.values())
        assert all(row["started_since_generation_ms"] >= 0 for row in faults.values())
        if recovery:
            assert {row["request_sha256"] for row in faults.values()} == {
                requests[0]["request_sha256"]
            }  # Frozen original question, evidence and token cap; no quick-answer expansion.
            assert sum(row.get("kind") == "answer_recovery" for row in result["metadata_logs"]) == 1
    else:
        assert not faults
    assert replay.TRIAL_ORIGIN.get() is None
    messages = [event["message"] for event in result["events"]]
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert len({m["question_id"] for m in messages if m.get("question_id")}) == 1


async def test_cancellation_before_injection_cannot_start_recovery_or_bill(tmp_path, monkeypatch):
    condition = variant(True, SyntheticFault(delay_ms=250))
    case = replay.load_cases()[0]
    raw = ScriptedProvider("openai")
    faults, hosts = {}, []
    armed = asyncio.Event()
    original_host = replay.Host

    def capture_host(*args):
        host = original_host(*args)
        hosts.append(host)
        return host

    def record(row):
        faults[row["fault_id"]] = row
        if row["status"] == "armed":
            armed.set()

    monkeypatch.setattr(replay, "Host", capture_host)
    router = build_router(condition.settings, [raw])
    replay.apply_synthetic_fault(router, condition, record)
    task = asyncio.create_task(
        replay.trial(case, condition.label, 0, condition.settings, router, 5)
    )
    await asyncio.wait_for(armed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not raw.calls
    assert faults
    assert all(r["status"] == "cancelled_before_injection" for r in faults.values())
    assert not any(r.get("kind") == "answer_recovery" for r in hosts[0].logs)
    assert not any(e["message"]["type"] == "answer.delta" for e in hosts[0].events)


@pytest.mark.parametrize("text", ["Incomplete phrase", "A complete first sentence. "])
async def test_after_first_text_failure_never_retries_as_expert_even_if_not_released(text):
    class PartialFailure:
        name = "openai"

        def __init__(self):
            self.models = []

        async def stream(self, model, req, params):
            self.models.append(model)
            yield text
            raise httpx2.ConnectError("offline failure after text")

    raw = PartialFailure()
    configuration = settings(True)
    result = await replay.trial(
        replay.load_cases()[0], "E", 0, configuration, build_router(configuration, [raw]), 5
    )
    assert result["failure"]
    assert raw.models == ["offline-fast"]
    assert not any(r.get("kind") == "answer_recovery" for r in result["metadata_logs"])


async def test_real_recovery_error_survives_synthetic_filter_and_keeps_unknown_cost(tmp_path):
    condition = variant(True, SyntheticFault())
    raw = ScriptedProvider(
        "openai", lambda *args: Reply([], error=httpx2.ReadError("offline read"))
    )
    budget = replay.AuditBudget(tmp_path, condition.settings, 1)
    requests, faults = [], {}
    router = build_router(
        condition.settings, [replay.AuditProvider(raw, budget, requests, tmp_path)]
    )
    replay.apply_synthetic_fault(
        router, condition, lambda row: faults.update({row["fault_id"]: row})
    )
    result = await replay.trial(replay.load_cases()[0], "E", 0, condition.settings, router, 5)
    replay.annotate_synthetic_faults(result, condition, list(faults.values()))
    assert not result["expected_injected_failure"]
    assert len(requests) == len(raw.calls) == len(budget.entries) == 1
    assert budget.entries[0]["status"] == "reserved"
    assert classify_failure(replay.real_failure_evidence(result), requests) == "transport"


async def test_four_label_schedule_preserves_all_expected_failures_and_metered_calls(
    tmp_path, monkeypatch
):
    conditions = {}
    for recovery in (False, True):
        for injected in (False, True):
            value = variant(recovery, SyntheticFault() if injected else None)
            value.label += "-fault" if injected else "-control"
            conditions[value.label] = value
    await replay.preflight(replay.load_cases()[:2], conditions)
    raw = ScriptedProvider("openai", lambda *args: Reply(["A complete answer."]))
    monkeypatch.setattr(replay, "make_providers", lambda *args, **kwargs: [raw])
    monkeypatch.setattr(replay, "experiment_identity", lambda *args: {"test": "fixed", "code": {}})
    args = Namespace(
        architectures=["D"],
        reps=2,
        seed=20261125,
        concurrency=2,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=2,
    )
    await replay.execute_live(args, replay.load_cases()[:2], settings(), None, conditions, tmp_path)
    results = json.loads((tmp_path / "trials.json").read_text())
    accounting = json.loads((tmp_path / "accounting.json").read_text())
    ledger = json.loads((tmp_path / "cost.json").read_text())
    assert len(results) == 16
    assert accounting["expected_injected_failures"] == 4
    assert accounting["successful_trials"] == 12
    assert accounting["failed_trials"] == 0
    assert accounting["circuit_reason"] is None
    assert len(ledger) == len(raw.calls) == 12
    assert accounting["injected_fault_attempts"] >= 8
    events = json.loads((tmp_path / "attempt-events.json").read_text())
    synthetic = [e for e in events if "-fault:" in e["trial_id"] and e["job"] == "fast"]
    assert synthetic
    assert all(e["synthetic_fault"] for e in synthetic)
    assert all(e["actual_provider_dispatched"] is False for e in synthetic)
    assert all(not e.get("synthetic_fault") for e in events if e["job"] == "answer_recovery")
    identity = json.loads((tmp_path / "identity.json").read_text())
    runs = replay.normalized_runs(results, replay.load_cases()[:2], identity, ledger)
    fault_rows = next(run for run in runs if run.architecture == "D-fault").rows
    assert all(row["expected_injected_failure"] for row in fault_rows)
    assert all(row["failure_kind_reclassified"] == "synthetic_pre_dispatch" for row in fault_rows)
