from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import asyncpg
import pytest

from copilot_core.workplace import Hit
from copilot_gateway.retrieval import CompanyMemory, Lookup
from copilot_gateway.session import Session


async def noop():
    pass


def host(memory: CompanyMemory | None) -> Session:
    session = object.__new__(Session)
    session.__dict__.update(
        company_memory=memory,
        manager=SimpleNamespace(forget=lambda _: None),
        seatdesk=SimpleNamespace(stop=noop),
        companiondesk=SimpleNamespace(stop=noop),
        _language_changes=set(),
        conv=SimpleNamespace(close=lambda: None),
        _settling=set(),
        _forced=set(),
        _forced_detections={},
        send_q=None,
        core_task=None,
        stt=None,
        stt_me=None,
        _warm_tasks=set(),
    )
    return session


def memory():
    return CompanyMemory(
        cast(asyncpg.Pool, None), uuid4(), uuid4(), embedding_model=None, budget_ms=200, limit=6
    )


@pytest.mark.asyncio
async def test_session_stop_closes_and_drains_unfinished_company_lookups():
    mem = memory()
    gate = asyncio.Event()

    async def pending() -> list[Hit]:
        await gate.wait()
        return []

    tasks = [asyncio.create_task(pending()) for _ in range(2)]
    mem.lookups["q"] = Lookup("q", time.monotonic(), tasks[0], tasks[1])
    await asyncio.sleep(0)
    try:
        await Session._stop(host(mem))
        assert not mem.lookups
        assert all(task.cancelled() for task in tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_drain_has_a_bound_even_if_query_delays_cancellation(caplog):
    mem = memory()
    started = asyncio.Event()
    release = asyncio.Event()

    async def reluctant() -> list[Hit]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return []

    task = asyncio.create_task(reluctant())

    async def cooperative() -> list[Hit]:
        await asyncio.Event().wait()
        return []

    normal = asyncio.create_task(cooperative())
    mem.lookups["q"] = Lookup("q", time.monotonic(), task, normal)
    await started.wait()
    try:
        await asyncio.wait_for(Session._stop(host(mem)), 0.6)
        assert not task.done()
        assert normal.cancelled()
        assert "drain bound" in caplog.text
    finally:
        release.set()
        normal.cancel()
        await asyncio.gather(task, normal, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_without_company_or_with_finished_lookup_is_unchanged():
    await Session._stop(host(None))
    mem = memory()

    async def finished() -> list[Hit]:
        return []

    a = asyncio.create_task(finished())
    b = asyncio.create_task(finished())
    await asyncio.gather(a, b)
    mem.lookups["q"] = Lookup("q", time.monotonic(), a, b)
    await Session._stop(host(mem))
    assert not mem.lookups
