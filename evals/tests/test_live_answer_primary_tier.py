"""Reservation pricing honors route tiers without changing the audited wire request."""

from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from copilot_evals.live_answer_async_checkpoints import BufferedRecordStore, DurableWriter
from copilot_evals.live_answer_checkpoints import RecordDispatchJournal
from copilot_evals.live_answer_eval import fingerprint
from copilot_evals.live_answer_replay import AuditBudget, AuditProvider

from copilot_core.llm import LLMDone, LLMRequest, LLMUsage
from copilot_core.pricing import ModelPrice

MODEL = "gpt-6-luna"


class TierProbe:
    name = "openai"

    def __init__(self, budget: AuditBudget, served_tier: str):
        self.budget = budget
        self.served_tier = served_tier
        self.calls: list[dict[str, Any]] = []
        self.reservations: list[dict[str, Any]] = []

    def capture(self, model: str, params: Mapping[str, Any]) -> LLMDone:
        assert model == MODEL
        self.calls.append(deepcopy(dict(params)))
        self.reservations.append(deepcopy(self.budget.entries[-1]))
        return LLMDone(
            LLMUsage(input_tokens=100, cached_tokens=20, output_tokens=5, cache_write_tokens=0),
            "end_turn",
            self.name,
            model,
            service_tier=self.served_tier,
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        done = self.capture(model, params)
        yield "Authored answer."
        yield done

    async def prewarm(self, model: str, req: LLMRequest, params: Mapping[str, Any]) -> LLMDone:
        return self.capture(model, params)


async def invoke(provider: AuditProvider, operation: str, params: dict[str, Any]) -> None:
    request = LLMRequest([], "Authored question", 50)
    if operation == "stream":
        chunks = [chunk async for chunk in provider.stream(MODEL, request, params)]
        assert chunks[0] == "Authored answer."
        assert isinstance(chunks[-1], LLMDone)
    else:
        assert isinstance(await provider.prewarm(MODEL, request, params), LLMDone)


async def setup(tmp_path: Path, buffered: bool, served_tier: str):
    writer = DurableWriter(tmp_path) if buffered else None
    if writer is not None:
        await writer.ready()
    store = BufferedRecordStore(writer) if writer is not None else None
    if store is not None:
        journal = RecordDispatchJournal(store, ["authored:D:0"], set(), set())
        assert journal.start("authored:D:0")
    settings = SimpleNamespace(
        model_prices={MODEL: ModelPrice(input=1, cached=0.1, output=2)},
        tier_price_multipliers={"standard": 1, "fast": 3, "priority": 3},
    )
    budget = AuditBudget(tmp_path, settings, 1, store=store)
    raw = TierProbe(budget, served_tier)
    records: list[dict[str, Any]] = []
    provider = AuditProvider(raw, budget, records, tmp_path, store=store)
    return writer, budget, raw, records, provider


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("operation", ["stream", "prewarm"])
@pytest.mark.parametrize(
    ("requested_tier", "served_tier", "reservation_multiplier"),
    [
        (None, "standard", 1),
        ("default", "standard", 1),
        ("auto", "priority", 1),
        ("standard", "standard", 1),
        ("fast", "priority", 3),
        ("fast", "standard", 3),
    ],
)
async def test_route_tier_prices_reservation_but_returned_tier_prices_usage(
    tmp_path: Path,
    buffered: bool,
    operation: str,
    requested_tier: str | None,
    served_tier: str,
    reservation_multiplier: int,
) -> None:
    writer, budget, raw, records, provider = await setup(tmp_path, buffered, served_tier)
    params: dict[str, Any] = {"reasoning": {"effort": "none"}, "max_output_tokens_extra": 17}
    if requested_tier is not None:
        params["service_tier"] = requested_tier
    original = deepcopy(params)
    try:
        await invoke(provider, operation, params)
        assert raw.calls == [original]
        assert params == original
        reservation = raw.reservations[0]
        assert reservation["status"] == "reserved"
        assert reservation["reservation_output_tokens"] == (67 if operation == "stream" else 0)
        expected = (
            (
                Decimal(reservation["reservation_input_tokens"]) * Decimal("2.5")
                + Decimal(reservation["reservation_output_tokens"]) * 2
            )
            * reservation_multiplier
            / 1_000_000
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        assert Decimal(str(reservation["charged_usd"])) == expected
        assert len(budget.entries) == 1
        entry = budget.entries[0]
        assert entry["status"] == "usage"
        assert entry["tier"] == served_tier
        assert entry["charged_usd"] == (0.000276 if served_tier == "priority" else 0.000092)
        if operation == "stream" or buffered:
            assert len(records) == 1
            assert records[0]["provider_invoked"] is True
            assert records[0]["status"] == "completed"
        else:
            assert records == []  # Preserve the existing nonbuffered prewarm audit shape.
        if operation == "stream":
            assert records[0]["params_sha256"] == fingerprint(original)
    finally:
        if writer is not None:
            await writer.close()


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("operation", ["stream", "prewarm"])
async def test_unpriced_route_tier_never_invokes_provider(
    tmp_path: Path, buffered: bool, operation: str
) -> None:
    writer, budget, raw, records, provider = await setup(tmp_path, buffered, "standard")
    params = {"service_tier": "unpriced-authored-tier"}
    try:
        with pytest.raises(ValueError, match="Tier price missing; refusing billed call"):
            await invoke(provider, operation, params)
        assert raw.calls == []
        assert budget.entries == []
        assert params == {"service_tier": "unpriced-authored-tier"}
        if operation == "stream" or buffered:
            assert len(records) == 1
            assert records[0]["provider_invoked"] is False
    finally:
        if writer is not None:
            await writer.close()
