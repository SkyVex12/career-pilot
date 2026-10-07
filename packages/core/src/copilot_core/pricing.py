"""What a model call cost, from its token usage (ARCHITECTURE.md, "Session logging",
"Answer cost").

Prices are US dollars per million tokens, per model, from configuration
(`COPILOT_MODEL_PRICES`, JSON). A price is keyed by the model's name without provider
prefixes or version suffixes, so `claude-haiku-4-5`, `anthropic.claude-haiku-4-5` and
`us.anthropic.claude-haiku-4-5-20251001-v1:0` all use the `claude-haiku-4-5` price; an OpenAI
snapshot like `gpt-6-luna-2026-09-01` uses the `gpt-6-luna` price. The tier multiplies the
whole price: Priority costs 75% more, Flex and Batch half.

Only Claude prices are built in. OpenAI model names are configuration, so their prices are
too: COPILOT_MODEL_PRICES in .env (.env.example has the list prices of its suggested models).
A model without a price is logged at startup (`missing_prices`) and its cost stays empty.

`input_tokens` in `LLMUsage` counts every input token, cached ones included; Claude cache writes
are not kept apart, so they count as plain input (the real price is 1.25x, a small
undercount). OpenAI's `input_tokens` already counts cached tokens and has no write surcharge.
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
}

DEFAULT_TIERS: dict[str, float] = {"standard": 1.0, "priority": 1.75, "flex": 0.5, "batch": 0.5}

_PREFIX = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic\.)?")
_SUFFIX = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})?(?:-v\d+(?::\d+)?)?$")
SIX_PLACES = Decimal("0.000001")  # answers.cost_usd is numeric(10,6)


def model_key(model: str) -> str:
    """`us.anthropic.claude-haiku-4-5-20251001-v1:0` to `claude-haiku-4-5`."""
    return _SUFFIX.sub("", _PREFIX.sub("", model.strip().lower()))


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
) -> Decimal | None:
    """The call's cost, rounded to six places; None when the model or tier has no price."""
    price = prices.get(model_key(model))
    multiplier = tiers.get(tier)
    if price is None or multiplier is None:
        return None
    cached = max(0, min(cached_tokens, input_tokens))
    total = (
        Decimal(str(price.input)) * (input_tokens - cached)
        + Decimal(str(price.cached)) * cached
        + Decimal(str(price.output)) * max(0, output_tokens)
    ) * Decimal(str(multiplier))
    return (total / Decimal(1_000_000)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
