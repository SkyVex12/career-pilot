"""Answer cost from token usage (copilot_core.pricing)."""

from decimal import Decimal

import pytest

from copilot_core.config import CoreSettings
from copilot_core.pricing import DEFAULT_PRICES, DEFAULT_TIERS, cost_usd, model_key


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("anthropic.claude-sonnet-5", "claude-sonnet-5"),
        ("Claude-Sonnet-5-5", "claude-sonnet-5-5"),
    ],
)
def test_model_key_drops_provider_prefixes_and_versions(model: str, key: str) -> None:
    assert model_key(model) == key


def test_uncached_cached_and_output_tokens_each_have_their_price() -> None:
    # Haiku 4.5: 200 uncached at $1, 4,000 cache reads at $0.10, 40 output at $5 per MTok.
    cost = cost_usd("claude-haiku-4-5", "standard", 4200, 4000, 40, DEFAULT_PRICES, DEFAULT_TIERS)
    assert cost == Decimal("0.000800")
    # Sonnet 5.5 on Bedrock, same usage: $2 / $0.20 / $10.
    cost = cost_usd(
        "anthropic.claude-sonnet-5-5", "standard", 4200, 4000, 40, DEFAULT_PRICES, DEFAULT_TIERS
    )
    assert cost == Decimal("0.001600")


def test_the_tier_multiplies_the_price() -> None:
    args = ("claude-haiku-4-5", 1_000_000, 0, 0, DEFAULT_PRICES, DEFAULT_TIERS)
    std, prio, flex = (cost_usd(args[0], t, *args[1:]) for t in ("standard", "priority", "flex"))
    assert (std, prio, flex) == (Decimal("1.000000"), Decimal("1.750000"), Decimal("0.500000"))


def test_unknown_model_or_tier_has_no_cost() -> None:
    assert cost_usd("prepared", "standard", 0, 0, 0, DEFAULT_PRICES, DEFAULT_TIERS) is None
    assert cost_usd("claude-haiku-4-5", "spot", 10, 0, 1, DEFAULT_PRICES, DEFAULT_TIERS) is None


def test_more_cached_than_input_tokens_is_clamped() -> None:
    cost = cost_usd("claude-haiku-4-5", "standard", 100, 500, 0, DEFAULT_PRICES, DEFAULT_TIERS)
    assert cost == Decimal("0.000010")  # 100 cache reads at $0.10


def test_configured_prices_merge_over_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "COPILOT_MODEL_PRICES",
        '{"anthropic.claude-haiku-4-5": {"input": 1.1, "cached": 0.11, "output": 5.5}}',
    )
    monkeypatch.setenv("COPILOT_TIER_PRICE_MULTIPLIERS", '{"priority": 2}')
    s = CoreSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.model_prices["claude-haiku-4-5"].output == 5.5
    assert s.model_prices["claude-opus-5-5"] == DEFAULT_PRICES["claude-opus-5-5"]
    assert s.tier_price_multipliers == {**DEFAULT_TIERS, "priority": 2.0}
