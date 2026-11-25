"""Offline regression coverage for incomplete token-cost telemetry."""

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from copilot_evals import answer_eval, coding_eval, company_eval, knowledge_eval, session_eval

from copilot_core.llm import Decided, LLMDone, LLMUsage
from copilot_core.pricing import DEFAULT_TIERS, ModelPrice


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        model_prices={"gpt-6-luna": ModelPrice(input=1, cached=0.1, output=5)},
        tier_price_multipliers=DEFAULT_TIERS,
        detector_api="decisions",
        decisions_model="gpt-6-luna",
    )


def completion(known: bool) -> LLMDone:
    return LLMDone(
        usage=LLMUsage(
            input_tokens=12000,
            cached_tokens=4000,
            output_tokens=20,
            cache_write_tokens=7000,
            cache_write_30m_tokens=3000 if known else None,
            cache_write_12h_tokens=4000 if known else None,
            reasoning_tokens=7,
        ),
        stop_reason="end_turn",
        model="gpt-6-luna",
        provider="openai",
    )


def test_call_cost_prices_observed_writes_without_double_charging_reasoning() -> None:
    assert answer_eval.call_cost(settings(), completion(True)) == pytest.approx(0.015250)
    assert answer_eval.call_cost(settings(), completion(False)) is None
    assert answer_eval.call_cost(settings(), None) is None


def test_mixed_costs_keep_known_subtotal_and_unknown_total_through_aggregation() -> None:
    row: dict[str, Any] = {"cost_usd": 0.0, "pass": "fast"}
    answer_eval.add_cost(row, 0.1)
    answer_eval.add_cost(row, None)
    answer_eval.add_cost(row, 0.2)
    total = answer_eval.summarize([row, {"pass": "deep", "cost_usd": 0.04}])
    assert total["cost_usd"] is None
    assert total["known_cost_usd"] == 0.34
    assert total["unpriced_cost_entries"] == 1
    assert "unknown" in answer_eval.format_cost(total)
    assert "$0.340000" in answer_eval.format_cost(total)


def test_zero_cost_and_missing_cost_are_distinct() -> None:
    assert answer_eval.summarize_costs([{"cost_usd": 0.0}]) == {
        "cost_usd": 0.0,
        "known_cost_usd": 0.0,
        "unpriced_cost_entries": 0,
    }
    assert answer_eval.summarize_costs([{}]) == {
        "cost_usd": None,
        "known_cost_usd": 0.0,
        "unpriced_cost_entries": 1,
    }
    assert answer_eval.summarize_costs([])["cost_usd"] == 0.0


def test_coding_and_company_eval_keep_missing_costs() -> None:
    subtotal = coding_eval.call_costs(settings(), [completion(True), completion(False)])
    assert subtotal["cost_usd"] is None
    assert subtotal["known_cost_usd"] == pytest.approx(0.015250)
    assert subtotal["unpriced_cost_entries"] == 1
    total = company_eval.summarize([{"mode": "standup", "pass": "fast", **subtotal}])
    assert total == subtotal


def test_knowledge_legacy_cost_key_retains_unknown_and_known_subtotal() -> None:
    row: dict[str, Any] = {"cost": 0.0, "fired": False, "source": "none"}
    answer_eval.add_cost(row, None, "cost")
    answer_eval.add_cost(row, 0.02, "cost")
    total = knowledge_eval.summarize([row])
    assert total["cost_usd"] is None
    assert total["known_cost_usd"] == 0.02
    assert total["unpriced_cost_entries"] == 1
    assert "unknown" in dict(session_eval.summary_rows(total))["Cost (USD)"]


@pytest.mark.parametrize("api", ["decisions", "responses"])
@pytest.mark.parametrize("known", [True, False])
async def test_session_detector_returns_nullable_cost_with_cache_write_usage(
    api: str, known: bool
) -> None:
    done = completion(known)

    class Provider:
        async def decide(self, *_args: Any) -> Decided:
            return Decided([], done.usage, "openai", done.model)

        async def stream(self, *_args: Any) -> AsyncGenerator[str | LLMDone]:
            yield '{"decision":"none","question_type":"other"}'
            yield done

    config = settings()
    config.detector_api = api
    fixture = session_eval.load_fixtures()[0]
    _detection, _elapsed, cost = await session_eval.detect_line(
        Provider(),
        done.model,
        fixture,
        fixture.lines[0],
        "Alex Rivera",
        config,
    )
    if known:
        # Decisions bills input only; Responses includes all reported output.
        assert cost == pytest.approx(0.015150 if api == "decisions" else 0.015250)
    else:
        assert cost is None
