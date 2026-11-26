"""Answer cost from token usage (copilot_core.pricing)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from copilot_core.config import CoreSettings
from copilot_core.pricing import (
    DEFAULT_PRICES,
    DEFAULT_TIERS,
    ModelPrice,
    cost_usd,
    default_tiers,
    model_key,
)


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
    assert s.tier_price_multipliers == {**default_tiers(), "priority": 2.0}


@pytest.mark.parametrize("tier", ["fast", "priority"])
def test_openai_fast_alias_uses_model_specific_rate(tier: str) -> None:
    prices = {"gpt-6-luna": ModelPrice(input=0.1, cached=0.01, output=0.5)}
    # Include cache reads and output; a generic 1.75x rate would undercount.
    assert cost_usd(
        "gpt-6-luna-2026-09-01",
        tier,
        1_000_000,
        100_000,
        100_000,
        prices,
        DEFAULT_TIERS,
        cache_write_tokens=0,
    ) == Decimal("0.282000")
    custom = {**DEFAULT_TIERS, f"gpt-6-luna:{tier}": 3.0}
    assert cost_usd(
        "gpt-6-luna", tier, 1_000_000, 0, 0, prices, custom, cache_write_tokens=0
    ) == Decimal("0.300000")


def test_fast_does_not_guess_price_for_an_unverified_model() -> None:
    prices = {"custom-model": ModelPrice(input=1, cached=0.1, output=5)}
    assert cost_usd("custom-model", "fast", 100, 0, 10, prices, DEFAULT_TIERS) is None


@pytest.mark.parametrize(
    ("written", "short", "long", "expected"),
    [
        (0, None, None, "0.008500"),
        (0, 0, 0, "0.008500"),
        (7000, 7000, 0, "0.010250"),
        (7000, 0, 7000, "0.019000"),
        (7000, 3000, 4000, "0.015250"),
    ],
)
def test_openai_write_categories_are_disjoint_not_added_to_ordinary_input(
    written: int, short: int | None, long: int | None, expected: str
) -> None:
    # Official mixed-TTL example: 12,000 input, 4,000 reads, 7,000 writes.
    # 1,000 ordinary + 400 read + 3,750 short + 10,000 long + 100 output dollars/MTok.
    prices = {"gpt-6-luna": ModelPrice(input=1, cached=0.1, output=5)}
    assert cost_usd(
        "gpt-6-luna",
        "standard",
        12000,
        4000,
        20,
        prices,
        DEFAULT_TIERS,
        cache_write_tokens=written,
        cache_write_30m_tokens=short,
        cache_write_12h_tokens=long,
    ) == Decimal(expected)


@pytest.mark.parametrize(
    ("written", "short", "long"),
    [
        (None, None, None),
        (None, 3000, 4000),
        (7000, None, None),
        (7000, 3000, None),
        (7000, None, 4000),
        (7000, 3000, 3000),
        (9000, 5000, 4000),
        (0, 1, 0),
        (-1, 0, 0),
        (0, -1, 0),
        (True, 1, 0),
        (7000, 3000.0, 4000),
    ],
)
def test_unknown_or_inconsistent_openai_write_usage_stays_unpriced(
    written: int | None, short: int | None, long: int | None
) -> None:
    prices = {"gpt-6-luna": ModelPrice(input=1, cached=0.1, output=5)}
    assert (
        cost_usd(
            "gpt-6-luna",
            "standard",
            12000,
            4000,
            20,
            prices,
            DEFAULT_TIERS,
            cache_write_tokens=written,
            cache_write_30m_tokens=short,
            cache_write_12h_tokens=long,
        )
        is None
    )


def test_fast_tier_multiplies_all_disjoint_token_categories_once() -> None:
    prices = {"gpt-6-luna": ModelPrice(input=1, cached=0.1, output=5)}
    assert cost_usd(
        "gpt-6-luna-2026-09-01",
        "fast",
        12000,
        4000,
        20,
        prices,
        DEFAULT_TIERS,
        cache_write_tokens=7000,
        cache_write_30m_tokens=3000,
        cache_write_12h_tokens=4000,
    ) == Decimal("0.030500")


def test_legacy_openai_does_not_require_new_write_telemetry() -> None:
    prices = {"gpt-5.4-mini": ModelPrice(input=1, cached=0.1, output=5)}
    assert cost_usd(
        "gpt-5.4-mini",
        "standard",
        12000,
        4000,
        20,
        prices,
        DEFAULT_TIERS,
    ) == Decimal("0.008500")


@pytest.mark.parametrize(
    ("model", "billed"),
    [
        ("gpt-5.4-mini", False),
        ("gpt-5.5-pro", False),
        ("gpt-5.6", True),
        ("gpt-6-luna-2026-09-01", True),
        ("gpt-6.1-sol", True),
        ("gpt-6.2-astra", True),
        ("claude-sonnet-5-5", False),
        ("custom-model", False),
    ],
)
def test_cache_write_pricing_model_boundary(model: str, billed: bool) -> None:
    from copilot_core.pricing import openai_cache_write_pricing

    assert openai_cache_write_pricing(model) is billed


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6.2-sol", "gpt-6.2-astra"])
@pytest.mark.parametrize("tier", ["fast", "priority"])
def test_verified_new_model_fast_prices(model: str, tier: str) -> None:
    prices = {model: ModelPrice(input=1, cached=0.1, output=5)}
    assert cost_usd(
        model,
        tier,
        12000,
        4000,
        20,
        prices,
        DEFAULT_TIERS,
        cache_write_tokens=7000,
        cache_write_30m_tokens=3000,
        cache_write_12h_tokens=4000,
    ) == Decimal("0.030500")


def test_selected_expert_is_priced_and_account_rates_override_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COPILOT_MODEL_PRICES", raising=False)
    monkeypatch.delenv("COPILOT_TIER_PRICE_MULTIPLIERS", raising=False)
    settings = CoreSettings(_env_file=None)  # pyright: ignore[reportCallIssue]

    def estimate(s: CoreSettings) -> Decimal | None:
        # 100 ordinary input, 100 30m writes, 4,000 reads, 40 output, Fast tier.
        return cost_usd(
            "gpt-6.2-sol",
            "fast",
            4200,
            4000,
            40,
            s.model_prices,
            s.tier_price_multipliers,
            cache_write_tokens=100,
            cache_write_30m_tokens=100,
            cache_write_12h_tokens=0,
        )

    assert estimate(settings) == Decimal("0.002500")
    monkeypatch.setenv(
        "COPILOT_MODEL_PRICES",
        '{"gpt-6.2-sol": {"input": 3, "cached": 0.15, "output": 15}}',
    )
    custom = CoreSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert estimate(custom) == Decimal("0.003750")
    assert (
        cost_usd(
            "gpt-6.2-sol",
            "fast",
            4200,
            4000,
            40,
            custom.model_prices,
            custom.tier_price_multipliers,
        )
        is None
    )  # Missing cache-write telemetry is still unknown, never assumed zero.


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6.2-astra"])
@pytest.mark.parametrize(
    ("at", "multiplier"),
    [
        (datetime(2027, 3, 1, 7, 59, 59, tzinfo=UTC), 3),
        (datetime(2027, 3, 1, 8, tzinfo=UTC), 6),
    ],
)
def test_ultrafast_promotion_boundary_and_disjoint_usage(model, at, multiplier):
    rates = default_tiers(at)
    assert rates[f"{model}:ultrafast"] == multiplier
    assert rates["gpt-6.1-sol:ultrafast"] == 6
    assert rates["gpt-6-astra:ultrafast"] == 6
    assert "gpt-6-luna:ultrafast" not in rates
    assert (
        cost_usd(
            model,
            "ultrafast",
            12000,
            4000,
            20,
            {model: ModelPrice(input=1, cached=0.1, output=5)},
            rates,
            cache_write_tokens=7000,
            cache_write_30m_tokens=3000,
            cache_write_12h_tokens=4000,
        )
        == Decimal("0.015250") * multiplier
    )


def test_ultrafast_explicit_account_rates_win(monkeypatch):
    monkeypatch.setenv("COPILOT_TIER_PRICE_MULTIPLIERS", '{"gpt-6.2-sol:ultrafast": 4.5}')
    settings = CoreSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.tier_price_multipliers["gpt-6.2-sol:ultrafast"] == 4.5
