"""What a model call cost, from its token usage (ARCHITECTURE.md, "Session logging",
"Answer cost").

Prices are US dollars per million tokens, per model, from configuration
(`COPILOT_MODEL_PRICES`, JSON). A price is keyed by the model's name without provider
prefixes or version suffixes, so `claude-haiku-4-5`, `anthropic.claude-haiku-4-5` and
`us.anthropic.claude-haiku-4-5-20251001-v1:0` all use the `claude-haiku-4-5` price; an OpenAI
snapshot like `gpt-6-luna-2026-09-01` uses the `gpt-6-luna` price. The tier multiplies the
whole price. Model-specific keys such as `gpt-6-luna:fast` override generic tiers;
there is no universal Priority/Fast multiplier. Flex and Batch default to half.

Claude prices and the selected adaptive expert model are built in. Other OpenAI models
use COPILOT_MODEL_PRICES in .env (.env.example lists its suggested models). Configuration
overrides the built-in rates. Unknown models stay unpriced and are logged at startup.

`input_tokens` includes ordinary input, cache reads, and writes. GPT-5.6+ writes are
separate, disjoint input categories: 30-minute writes cost 1.25x input and 12-hour writes
2.5x, instead of ordinary input (not added on top). Missing write telemetry leaves these
models unpriced. Older OpenAI models have no additional cache-write charge. Claude writes
are still not kept apart and count as plain input, so their cost remains an underestimate.
Verified 2026-11-25: https://developers.openai.com/api/docs/guides/prompt-caching
"""

import re
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: float
    cached: float  # cache reads
    output: float


# Claude API list prices, checked October 2026. Bedrock prices may differ: set
# COPILOT_MODEL_PRICES for the account's.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(input=1.0, cached=0.10, output=5.0),
    "claude-sonnet-5-5": ModelPrice(input=2.0, cached=0.20, output=10.0),
    "claude-sonnet-5": ModelPrice(input=2.0, cached=0.20, output=10.0),
    "claude-opus-5-5": ModelPrice(input=4.0, cached=0.20, output=20.0),
    # Selected live expert, short-context Standard rates verified 2026-11-26:
    # https://developers.openai.com/api/docs/pricing.md
    "gpt-6.2-sol": ModelPrice(input=2.0, cached=0.10, output=10.0),
}

# OpenAI Fast is the renamed Priority tier; these model rates are 2x Standard.
# Verified 2026-11-25: https://developers.openai.com/api/docs/pricing.md
# Keep unknown models/tiers unpriced instead of applying an invented multiplier.
DEFAULT_TIERS: dict[str, float] = {
    "standard": 1.0,
    "priority": 1.75,
    "flex": 0.5,
    "batch": 0.5,
    **{
        f"{model}:{tier}": 2.0
        for model in (
            "gpt-6-luna",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6.2-sol",
            "gpt-6.2-astra",
        )
        for tier in ("fast", "priority")
    },
}

_PREFIX = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic\.)?")
_SUFFIX = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})?(?:-v\d+(?::\d+)?)?$")
_OPENAI_CACHE_WRITES = re.compile(r"^gpt-(?:5\.(?:[6-9]|\d{2,})|[6-9]|\d{2,})(?:[.\-]|$)")
SIX_PLACES = Decimal("0.000001")  # answers.cost_usd is numeric(10,6)


def model_key(model: str) -> str:
    """`us.anthropic.claude-haiku-4-5-20251001-v1:0` to `claude-haiku-4-5`."""
    return _SUFFIX.sub("", _PREFIX.sub("", model.strip().lower()))


def openai_cache_write_pricing(model: str) -> bool:
    """Whether this OpenAI model bills writes separately (GPT-5.6 and later)."""
    return bool(_OPENAI_CACHE_WRITES.match(model_key(model)))


def missing_prices(models: list[str], prices: dict[str, ModelPrice]) -> list[str]:
    """The models among these with no configured price."""
    return sorted({m for m in models if model_key(m) not in prices})


def cost_usd(
    model: str,
    tier: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    prices: dict[str, ModelPrice],
    tiers: dict[str, float],
    *,
    cache_write_tokens: int | None = None,
    cache_write_30m_tokens: int | None = None,
    cache_write_12h_tokens: int | None = None,
) -> Decimal | None:
    """Estimated token cost, or None for unknown rates or required write usage.

    GPT-5.6+ needs an observed aggregate write count. Positive writes also need
    both disjoint lifetime counts; neither a missing field nor request TTL proves
    its billed category. An observed zero aggregate is sufficient without TTLs.
    Legacy callers/models keep their prior calculation when writes are absent.
    """
    key = model_key(model)
    price = prices.get(key)
    multiplier = tiers.get(f"{key}:{tier}", tiers.get(tier))
    if price is None or multiplier is None:
        return None
    write_30m = write_12h = 0
    if openai_cache_write_pricing(key):
        counters = (cache_write_tokens, cache_write_30m_tokens, cache_write_12h_tokens)
        if any(v is not None and (type(v) is not int or v < 0) for v in counters):
            return None
        if cache_write_tokens is None:
            return None
        if cache_write_tokens:
            if cache_write_30m_tokens is None or cache_write_12h_tokens is None:
                return None
            write_30m, write_12h = cache_write_30m_tokens, cache_write_12h_tokens
        elif any(v not in (None, 0) for v in counters[1:]):
            return None
        if write_30m + write_12h != cache_write_tokens:
            return None
        if cached_tokens < 0 or cached_tokens + cache_write_tokens > input_tokens:
            return None
    elif any(
        v not in (None, 0)
        for v in (
            cache_write_tokens,
            cache_write_30m_tokens,
            cache_write_12h_tokens,
        )
    ):
        # These lifetime rates are OpenAI-specific; do not apply them to another
        # provider or an unknown model that happens to return similarly named fields.
        return None
    cached = max(0, min(cached_tokens, input_tokens))
    ordinary = input_tokens - cached - write_30m - write_12h
    total = (
        Decimal(str(price.input))
        * (ordinary + Decimal("1.25") * write_30m + Decimal("2.5") * write_12h)
        + Decimal(str(price.cached)) * cached
        + Decimal(str(price.output)) * max(0, output_tokens)
    ) * Decimal(str(multiplier))
    return (total / Decimal(1_000_000)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
