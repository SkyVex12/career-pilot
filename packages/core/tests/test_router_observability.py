"""No-network provider outcomes: distinguish retry failures from hedge/caller cancellation."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import httpx2
import openai
import pytest

from copilot_core.llm import (
    AttemptEvent,
    LLMChunk,
    LLMDone,
    LLMRequest,
    LLMUsage,
    ModelRouter,
    Route,
)

REQ = LLMRequest([], "source facts", 50)
TERMINAL = {"completed", "failed", "cancelled", "closed"}


class Provider:
    name = "test"

    def __init__(self, delay: float = 0, failure: Exception | None = None) -> None:
        self.delay = delay
        self.failure = failure
        self.calls = 0
        self.closed = 0
        self.started = asyncio.Event()

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            if self.failure:
                raise self.failure
            yield "Useful sentence."
            yield LLMDone(LLMUsage(20, 10, 3), "end_turn", service_tier="standard")
        finally:
            self.closed += 1


def terminals(events: list[AttemptEvent]) -> list[AttemptEvent]:
    result = [e for e in events if e.status in TERMINAL]
    assert len({e.attempt_id for e in result}) == len(result)
    return result


async def test_connect_timeout_and_usage_have_distinct_attempts_and_safe_causes() -> None:
    request = httpx2.Request("POST", "https://example.invalid/responses")
    cause = httpx2.ConnectTimeout("secret credential must not be logged", request=request)
    failure = openai.APITimeoutError(request=request)
    failure.__cause__ = cause
    slow, ready = Provider(failure=failure), Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter({"fast": [Route(slow, "m"), Route(ready, "m")]}, 1)
    chunks = [c async for c in router.stream("fast", REQ, on_attempt=events.append)]
    failed, completed = terminals(events)
    assert chunks[0] == "Useful sentence."
    assert failed.error_category == "connect_timeout"
    assert failed.cause_types == ("APITimeoutError", "ConnectTimeout")
    assert failed.usage is None
    assert "secret" not in repr(events)
    assert completed.usage == LLMUsage(20, 10, 3)
    assert completed.service_tier == "standard"
    assert completed.status == "completed"
    assert failed.request_id == completed.request_id
    assert failed.attempt_id != completed.attempt_id
    assert completed.since_request_ms >= completed.elapsed_ms
    assert (slow.closed, ready.closed) == (1, 1)


async def test_first_token_deadline_is_not_transport_or_caller_cancellation() -> None:
    slow, ready = Provider(delay=1), Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter(
        {"fast": [Route(slow, "m"), Route(ready, "m")]}, 0.02, on_attempt=events.append
    )
    await router.complete("fast", REQ)
    failed, completed = terminals(events)
    assert failed.status == "failed"
    assert failed.stage == "first_chunk"
    assert failed.error_category == "first_token_timeout"
    assert completed.status == "completed"
    assert (slow.calls, slow.closed, ready.calls, ready.closed) == (1, 1, 1, 1)


async def test_hedge_loser_is_unknown_usage_and_winner_completes_once() -> None:
    slow, ready = Provider(delay=1), Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter(
        {"fast": [Route(slow, "m"), Route(ready, "m")]},
        2,
        hedge_after_s={"fast": 0.01},
        on_attempt=events.append,
    )
    _, done = await router.complete("fast", REQ)
    by_attempt = {e.attempt: e for e in terminals(events)}
    assert by_attempt[0].status == "cancelled"
    assert by_attempt[0].error_category == "hedge_lost"
    assert by_attempt[0].usage is None
    assert by_attempt[1].status == "completed"
    assert done.attempt == 1
    assert done.hedged
    assert slow.closed == ready.closed == 1


async def test_caller_cancel_is_terminal_and_closes_pending_provider() -> None:
    provider = Provider(delay=10)
    events: list[AttemptEvent] = []
    router = ModelRouter({"fast": [Route(provider, "m")]}, 20, on_attempt=events.append)
    task = asyncio.create_task(router.complete("fast", REQ))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (cancelled,) = terminals(events)
    assert cancelled.status == "cancelled"
    assert cancelled.error_category == "caller_cancelled"
    assert cancelled.usage is None
    assert provider.closed == 1


async def test_consumer_closing_after_done_keeps_observed_usage() -> None:
    provider = Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter({"fast": [Route(provider, "m")]}, 1)
    stream = router.stream("fast", REQ, on_attempt=events.append)
    assert await anext(stream) == "Useful sentence."
    assert isinstance(await anext(stream), LLMDone)
    await stream.aclose()
    (completed,) = terminals(events)
    assert completed.status == "completed"
    assert completed.usage is not None
    assert provider.closed == 1


async def test_consumer_closing_partial_answer_does_not_retry() -> None:
    provider = Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter({"fast": [Route(provider, "m"), Route(provider, "m")]}, 1)
    stream = router.stream("fast", REQ, on_attempt=events.append)
    await anext(stream)
    await stream.aclose()
    (closed,) = terminals(events)
    assert closed.status == "closed"
    assert closed.error_category == "consumer_closed"
    assert closed.usage is None
    assert provider.calls == provider.closed == 1


async def test_stream_error_never_restarts_text_and_observer_failure_cannot_trigger_retry() -> None:
    class FailsAfterText(Provider):
        async def stream(
            self, model: str, req: LLMRequest, params: Mapping[str, Any]
        ) -> AsyncGenerator[LLMChunk]:
            self.calls += 1
            yield "Existing answer."
            raise TimeoutError("stream stalled")

    provider, spare = FailsAfterText(), Provider()
    events: list[AttemptEvent] = []

    def broken_observer(event: AttemptEvent) -> None:
        raise RuntimeError("telemetry unavailable")

    router = ModelRouter(
        {"fast": [Route(provider, "m"), Route(spare, "m")]}, 1, on_attempt=broken_observer
    )
    stream = router.stream("fast", REQ, on_attempt=events.append)
    assert await anext(stream) == "Existing answer."
    with pytest.raises(TimeoutError, match="stream stalled"):
        await anext(stream)
    (failed,) = terminals(events)
    assert failed.status == "failed"
    assert failed.stage == "stream"
    assert failed.error_category == "provider_timeout"
    assert spare.calls == 0


async def test_cancel_during_hedge_cleanup_also_closes_the_opened_winner() -> None:
    closing = asyncio.Event()

    class SlowClose(Provider):
        async def stream(
            self, model: str, req: LLMRequest, params: Mapping[str, Any]
        ) -> AsyncGenerator[LLMChunk]:
            try:
                await asyncio.sleep(10)
                yield "never"
            finally:
                closing.set()
                await asyncio.sleep(10)

    loser, winner = SlowClose(), Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter(
        {"fast": [Route(loser, "m"), Route(winner, "m")]},
        20,
        hedge_after_s={"fast": 0.01},
        on_attempt=events.append,
    )
    task = asyncio.create_task(router.complete("fast", REQ))
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert winner.closed == 1
    assert len(terminals(events)) == 2


async def test_provider_timeout_before_text_is_not_mislabeled_router_deadline() -> None:
    provider, ready = Provider(failure=TimeoutError("provider internal timeout")), Provider()
    events: list[AttemptEvent] = []
    router = ModelRouter(
        {"fast": [Route(provider, "m"), Route(ready, "m")]}, 10, on_attempt=events.append
    )
    await router.complete("fast", REQ)
    failed, completed = terminals(events)
    assert failed.error_category == "provider_timeout"
    assert failed.elapsed_ms < 1000
    assert completed.status == "completed"
