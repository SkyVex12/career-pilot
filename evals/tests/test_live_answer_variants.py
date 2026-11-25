"""Pinned-prompt and model variants must stay isolated, explicit, and correctly attributed."""

import asyncio
import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from copilot_evals.live_answer_eval import blind_pairs, comparable, question_usage, summarize
from copilot_evals.live_answer_replay import (
    CONFIG_FIELDS,
    Host,
    load_cases,
    normalized_runs,
    tier_overrides,
    trial,
)
from copilot_evals.live_answer_variants import (
    EvaluationEngine,
    adaptive_constant,
    load_rate_card,
    load_variants,
    validate_route_prices,
)

from copilot_core import prompts
from copilot_gateway.answer import Question
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.testing import Reply, ScriptedProvider


def test_pinned_prompt_parser_never_executes_code_or_unapproved_expressions():
    assert (
        adaptive_constant('_RULES = "rules"\nADAPTIVE_SYSTEM = "start" + _RULES + "end"')
        == "startrulesend"
    )
    with pytest.raises(ValueError, match="literal"):
        adaptive_constant('ADAPTIVE_SYSTEM = __import__("os").getcwd()')
    with pytest.raises(ValueError, match="literal"):
        adaptive_constant("_RULES = ADAPTIVE_SYSTEM\nADAPTIVE_SYSTEM = _RULES")
    # Unrelated executable code in a historical module is parsed but never evaluated.
    assert (
        adaptive_constant('raise RuntimeError("must not execute")\nADAPTIVE_SYSTEM = "safe"')
        == "safe"
    )


def test_manifest_validates_conditions_and_does_not_mutate_settings(tmp_path):
    base = Settings(_env_file=None, llm_providers=["openai"], openai_fast_model="base-model")
    path = tmp_path / "variants.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "variants": [
                    {
                        "label": "D-test",
                        "architecture": "adaptive_on_demand",
                        "settings": {
                            "openai_fast_model": "test-model",
                            "openai_deep_first_token_timeout_ms": 20000,
                        },
                        "expert_route_limit": 1,
                    }
                ],
            }
        )
    )
    variant = load_variants(path, base, tmp_path)["D-test"]
    assert base.openai_fast_model == "base-model"
    assert variant.settings.openai_fast_model == "test-model"
    assert variant.settings.openai_deep_first_token_timeout_ms == 20000
    assert variant.expert_route_limit == 1
    data = json.loads(path.read_text())
    data["variants"][0]["settings"]["openai_api_key"] = "must-not-be-a-manifest-setting"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="settings"):
        load_variants(path, base, tmp_path)


async def test_parallel_pinned_prompt_substitution_is_per_engine_and_preserves_context():
    before = prompts.ADAPTIVE_SYSTEM
    settings = Settings(
        _env_file=None,
        llm_providers=["openai"],
        answer_architecture="adaptive_on_demand",
        openai_fast_model="offline",
        openai_detector_model="offline",
        openai_deep_model="offline",
        openai_fast_reasoning="none",
    )
    captured = {}
    case = load_cases()[0]

    async def one(label, rules):
        def reply(job, request):
            captured[label] = asdict(request)
            return Reply(["A useful answer."])

        provider = ScriptedProvider("openai", reply)
        return await trial(case, label, 0, settings, build_router(settings, [provider]), 10, rules)

    results = await asyncio.gather(
        one("old", "Pinned rules for {user_name} in {mode}."), one("current", None)
    )
    assert all(not row["failure"] for row in results)
    assert all(
        any(event["message"]["type"] == "answer.done" for event in row["events"])
        for row in results
    )
    assert before == prompts.ADAPTIVE_SYSTEM
    assert captured["old"]["system"][0]["text"].startswith("Pinned rules")
    assert captured["old"]["system"][1:] == captured["current"]["system"][1:]
    assert captured["old"]["content"] == captured["current"]["content"]
    assert captured["old"]["max_tokens"] == captured["current"]["max_tokens"]
    identity = {
        "strategies": {"D-v2": "old", "D-v3": "current"},
        "variants": {
            "old": {"architecture": "adaptive_on_demand"},
            "current": {"architecture": "adaptive_on_demand"},
        },
    }
    ledger = [
        {
            "trial_id": f"{case['id']}:old:0",
            "status": "usage",
            "charged_usd": 0.01,
            "model": "offline",
        }
    ]
    records = normalized_runs(results, [case], identity, ledger)
    assert comparable(records[0], records[1])["comparable"]
    assert records[0].identity["answer_architecture"] == "adaptive_on_demand"
    assert records[0].identity["condition_id"] == "old"
    assert (
        question_usage(records[0], records[0].rows[0])["all_request_ledger"][
            "provider_usage_priced_usd"
        ]
        == 0.01
    )
    assert summarize(records[0])["variant_label"] == "D-v2"
    review, _ = blind_pairs(records, 1, 4, baseline="D-v2")
    assert len(review["items"]) == 1
    assert "D-v2" not in json.dumps(review)


@pytest.mark.parametrize("pass_", ["fast", "deep"])
def test_pinned_prompt_uses_original_request_snapshot_after_context_changes(pass_):
    settings = Settings(
        _env_file=None, llm_providers=["openai"],
        answer_architecture="adaptive_on_demand",
        openai_fast_model="offline", openai_detector_model="offline", openai_deep_model="offline",
    )
    provider = ScriptedProvider("openai")
    case = {
        **load_cases()[0],
        "mode": "interview",
        "question": "Why did you choose Redis for the original project?",
        "digest": "Alex built the original project with Redis.",
        "recent": "Interviewer: Discuss the original project.",
    }
    host = Host(settings, build_router(settings, [provider]), case)
    engine = EvaluationEngine(host, "Pinned rules for {user_name} in {mode}.")
    question = Question(
        qid=uuid4(), text=case["question"], qtype="technical", confidence=1,
        addressed=True, speculative=False, forced=False, t0=time.monotonic(),
        detector_ms=None, match=None,
    )
    snapshot = engine._request_snapshot(question)
    question.text = "Describe the replacement project."
    case.update(
        mode="standup", digest="New unrelated profile.",
        recent="New unrelated conversation.",
    )
    request = engine._request(
        question, pass_, ["Original retrieved evidence."], snapshot=snapshot,
    )
    serialized = json.dumps(asdict(request))
    assert request.system[0].text.startswith("Pinned rules for Alex Rivera in an interview.")
    assert snapshot.text in serialized
    assert snapshot.context.digest_text in serialized
    assert snapshot.transcript.recent in serialized
    assert "Original retrieved evidence." in serialized
    assert "replacement project" not in serialized
    assert "New unrelated" not in serialized


def test_private_draft_is_inert_hashed_and_exclusive_with_commit(tmp_path):
    prompt = tmp_path / "draft.py"
    prompt.write_text('raise RuntimeError("never run")\nADAPTIVE_SYSTEM = "Draft rules"')
    path = tmp_path / "variants.json"
    data = {
        "schema": 1,
        "variants": [
            {"label": "D-draft", "architecture": "adaptive_on_demand", "prompt_file": "draft.py"}
        ],
    }
    path.write_text(json.dumps(data))
    variant = load_variants(path, Settings(_env_file=None), tmp_path)["D-draft"]
    assert variant.prompt_rules == "Draft rules"
    assert len(variant.prompt_identity["source_sha256"]) == 64
    data["variants"][0]["prompt_commit"] = "772f4ce"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="either"):
        load_variants(path, Settings(_env_file=None), tmp_path)


def test_rate_card_merges_without_mutating_base_and_rejects_nonfinite_rates(tmp_path):
    base = Settings(_env_file=None)
    path = tmp_path / "rates.json"
    data = {
        "schema": 1,
        "verified_at": "2026-11-25",
        "sources": ["https://developers.openai.com/api/docs/pricing.md"],
        "model_prices": {"gpt-6-sol": {"input": 2, "cached": 0.2, "output": 10}},
        "tier_price_multipliers": {"gpt-6-sol:fast": 2},
    }
    path.write_text(json.dumps(data))
    updated, identity = load_rate_card(base, path)
    assert updated.model_prices["gpt-6-sol"].input == 2
    assert set(base.model_prices) <= set(updated.model_prices)
    assert identity["verified_at"] == "2026-11-25"
    data["model_prices"]["gpt-6-sol"]["input"] = float("nan")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="finite"):
        load_rate_card(base, path)


def test_unpriced_routes_fail_before_billed_schedule():
    settings = Settings(
        _env_file=None,
        llm_providers=["openai"],
        openai_fast_model="unpriced",
        openai_detector_model="unpriced",
        openai_deep_model="unpriced",
    )
    provider = ScriptedProvider("openai", lambda job, request: Reply(["never called"]))
    with pytest.raises(ValueError, match="rate missing"):
        validate_route_prices(build_router(settings, [provider]), settings)


def test_recovery_variant_is_hashed_priced_and_keeps_one_expert_route(tmp_path):
    base = Settings(
        _env_file=None, llm_providers=["openai"],
        openai_fast_model="gpt-6-luna", openai_detector_model="gpt-6-luna",
        openai_deep_model="gpt-6.1-sol",
    )
    path = tmp_path / "recovery.json"
    path.write_text(json.dumps({"schema": 1, "variants": [{
        "label": "E", "architecture": "adaptive_on_demand",
        "settings": {"adaptive_failure_recovery": True},
        "expert_tier": "fast", "expert_route_limit": 1,
    }]}))
    variant = load_variants(path, base, tmp_path)["E"]
    assert not base.adaptive_failure_recovery
    assert variant.identity(CONFIG_FIELDS)["settings"]["adaptive_failure_recovery"] is True
    router = build_router(variant.settings, [ScriptedProvider("openai")])
    tier_overrides(router, variant)
    variant.limit_routes(router)
    assert len(router.routes["answer_recovery"]) == 1
    assert router.routes["answer_recovery"][0].params["service_tier"] == "fast"
    assert "answer_recovery" not in router.hedge_after_s
    # An unpriced recovery cannot hide behind otherwise priced primary/expert routes.
    router.routes = {**router.routes, "answer_recovery": [
        replace(router.routes["answer_recovery"][0], model="unpriced-recovery")
    ]}
    with pytest.raises(ValueError, match="rate missing"):
        validate_route_prices(router, variant.settings)
