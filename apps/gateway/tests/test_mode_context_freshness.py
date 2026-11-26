"""Actual Session methods; authored employer-pack returns, no providers or network."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMUsage
from copilot_gateway.session import Session

pytestmark = pytest.mark.asyncio


def host() -> Any:
    obj = cast(Any, object.__new__(Session))
    obj.ending = obj.lost = obj._retention_failed = False
    obj._warm = None
    obj._warm_tasks = set()
    obj.info = SimpleNamespace(
        mode="interview",
        employer_id="fictional-employer",
        title="role",
        agenda="authored",
        material_ids=[],
    )
    obj.principal = SimpleNamespace(user_id="fixture-user")
    obj.applied = []
    obj.set_pack = lambda pack: obj.applied.append(pack.label)
    obj._build_prompt_ctx = lambda: tuple(obj.applied)
    obj.load_sections = AsyncMock()
    obj.prewarm_prompts = AsyncMock()
    return obj


@pytest.mark.parametrize("modes", [("standup", "meeting"), ("standup", "meeting", "standup")])
async def test_old_mode_pack_cannot_overwrite_latest_mode(modes: tuple[str, ...]) -> None:
    obj = host()
    gates = [asyncio.Future() for _ in modes]
    entered = [asyncio.Event() for _ in modes]
    request = 0

    async def employer_pack(*_: Any) -> Any:
        nonlocal request
        index = request
        request += 1
        entered[index].set()
        return await gates[index]

    obj.manager = SimpleNamespace(employer_pack=employer_pack)
    tasks = []
    for index, mode in enumerate(modes):
        obj.info.mode = mode
        obj._start_warm(lambda mode=mode: obj._mode_changed(mode))
        tasks.append(obj._warm)
        await entered[index].wait()
    try:
        for index in reversed(range(len(modes))):
            gates[index].set_result(SimpleNamespace(label=f"revision-{index}"))
            await tasks[index]
        assert obj.applied == [f"revision-{len(modes) - 1}"]
        assert obj.prewarm_prompts.await_count == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancellation_suppressing_pack_return_cannot_mutate_after_end() -> None:
    obj = host()
    gate = asyncio.Future()
    entered = asyncio.Event()

    async def employer_pack(*_: Any) -> Any:
        entered.set()
        try:
            return await asyncio.shield(gate)
        except asyncio.CancelledError:
            return await gate

    obj.manager = SimpleNamespace(employer_pack=employer_pack)
    obj.info.mode = "standup"
    obj._start_warm(lambda: obj._mode_changed("standup"))
    task = obj._warm
    await entered.wait()
    obj.ending = True
    obj._cancel_warm()
    await asyncio.sleep(0)
    gate.set_result(SimpleNamespace(label="late"))
    await task
    assert obj.applied == []
    obj.prewarm_prompts.assert_not_awaited()


# These fixtures contain only authored data. Providers, stores, and thread work are stubbed.


def complete_host() -> Any:
    obj = host()
    obj.info.mode = "standup"
    obj.sections = None
    obj.sections_total = 0
    obj.svc = SimpleNamespace(
        store=SimpleNamespace(get_knowledge=AsyncMock()),
        jobs=SimpleNamespace(send=AsyncMock()),
    )
    obj.router = SimpleNamespace(warm=AsyncMock(), prewarm=AsyncMock())
    obj._prompt_ctx = prompts.PromptContext("Alex", "standup", None, None)
    obj.settings = SimpleNamespace(
        answer_architecture="legacy_dual",
        prewarm_jobs=["fast", "deep", "deep_light"],
        fast_pass_max_tokens=50,
        deep_pass_max_tokens=100,
    )
    obj.records = []
    obj.log_entry = obj.records.append
    obj._cost = lambda _: 0.001
    return obj


def returned_usage() -> LLMDone:
    return LLMDone(
        LLMUsage(
            input_tokens=12,
            cache_write_tokens=0,
            cache_write_30m_tokens=0,
            cache_write_12h_tokens=0,
        ),
        "prewarm",
        "authored",
        "authored-model",
    )


async def newer_task(obj: Any) -> None:
    async def settled() -> None:
        return None

    obj._start_warm(settled)
    await obj._warm


async def test_queued_obsolete_mode_does_not_request_a_pack() -> None:
    obj = complete_host()
    obj.manager = SimpleNamespace(employer_pack=AsyncMock(return_value=None))
    obj._start_warm(lambda: obj._mode_changed("standup"))
    old = obj._warm
    obj.info.mode = "meeting"
    obj._start_warm(lambda: obj._mode_changed("meeting"))
    latest = obj._warm
    await asyncio.gather(old, latest)
    assert obj.manager.employer_pack.await_count == 1
    assert obj.manager.employer_pack.call_args.args[2] == "meeting"
    obj.prewarm_prompts.assert_awaited_once()


@pytest.mark.parametrize("stage", ["knowledge", "index"])
@pytest.mark.parametrize("stop", ["superseded", "ending", "lost", "_retention_failed", "cancelled"])
async def test_late_section_result_does_not_publish_or_continue_warming(
    stage: str,
    stop: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj = complete_host()
    del obj.load_sections
    entered, release = asyncio.Event(), asyncio.Event()
    live = SimpleNamespace(chunks=["authored-section"])
    candidate_index = object()

    async def held(value: Any) -> Any:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return value

    async def get_knowledge(*_: Any) -> Any:
        return await held(live) if stage == "knowledge" else live

    async def build(*_: Any) -> Any:
        return await held(candidate_index) if stage == "index" else candidate_index

    obj.svc.store.get_knowledge = get_knowledge
    monkeypatch.setattr(asyncio, "to_thread", build)
    obj.warm_models()
    old = obj._warm
    await entered.wait()
    if stop == "superseded":
        await newer_task(obj)
    elif stop == "cancelled":
        old.cancel()
        await asyncio.sleep(0)
    else:
        setattr(obj, stop, True)
    release.set()
    await old
    assert obj.sections is None
    assert obj.sections_total == 0
    obj.router.warm.assert_not_awaited()
    obj.prewarm_prompts.assert_not_awaited()
    obj.svc.jobs.send.assert_not_awaited()


@pytest.mark.parametrize("stale", [False, True])
async def test_missing_sections_only_queue_publish_for_current_warm(stale: bool) -> None:
    obj = complete_host()
    del obj.load_sections
    entered, release = asyncio.Event(), asyncio.Event()

    async def missing(*_: Any) -> None:
        entered.set()
        await release.wait()

    obj.svc.store.get_knowledge = missing
    obj.warm_models()
    old = obj._warm
    await entered.wait()
    if stale:
        await newer_task(obj)
    release.set()
    await old
    assert obj.svc.jobs.send.await_count == (0 if stale else 1)
    assert obj.router.warm.await_count == (0 if stale else 1)
    assert obj.prewarm_prompts.await_count == (0 if stale else 1)


async def test_current_sections_publish_and_initial_warm_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj = complete_host()
    del obj.load_sections
    obj.svc.store.get_knowledge.return_value = SimpleNamespace(chunks=["first", "second"])
    candidate_index = object()
    build = AsyncMock(return_value=candidate_index)
    monkeypatch.setattr(asyncio, "to_thread", build)
    obj.warm_models()
    await obj._warm
    assert obj.sections is candidate_index
    assert obj.sections_total == 2
    obj.router.warm.assert_awaited_once()
    obj.prewarm_prompts.assert_awaited_once()


@pytest.mark.parametrize("stop", ["superseded", "ending", "cancelled"])
async def test_late_connection_warm_does_not_start_prompt_warm(stop: str) -> None:
    obj = complete_host()
    entered, release = asyncio.Event(), asyncio.Event()

    async def warm() -> None:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    obj.router.warm = warm
    obj.warm_models()
    old = obj._warm
    await entered.wait()
    if stop == "superseded":
        await newer_task(obj)
    elif stop == "cancelled":
        old.cancel()
        await asyncio.sleep(0)
    else:
        obj.ending = True
    release.set()
    await old
    obj.prewarm_prompts.assert_not_awaited()


@pytest.mark.parametrize("stop", ["superseded", "ending", "lost", "_retention_failed", "cancelled"])
@pytest.mark.parametrize("with_usage", [False, True])
async def test_remaining_prewarm_jobs_stop_but_returned_usage_is_preserved(
    stop: str,
    with_usage: bool,
) -> None:
    obj = complete_host()
    del obj.prewarm_prompts
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def prewarm(job: str, _: Any) -> LLMDone | None:
        calls.append(job)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return returned_usage() if with_usage else None

    obj.router.prewarm = prewarm
    obj._start_warm(obj.prewarm_prompts)
    old = obj._warm
    await entered.wait()
    if stop == "superseded":
        await newer_task(obj)
    elif stop == "cancelled":
        old.cancel()
        await asyncio.sleep(0)
    else:
        setattr(obj, stop, True)
    release.set()
    await old
    assert calls == ["fast"]
    assert len(obj.records) == int(with_usage)
    if with_usage:
        assert obj.records[0]["input_tokens"] == 12
        assert obj.records[0]["cost_usd"] == 0.001
        assert obj.records[0]["pass"] == "fast"


async def test_current_prewarm_runs_all_configured_jobs() -> None:
    obj = complete_host()
    del obj.prewarm_prompts
    obj.router.prewarm.return_value = returned_usage()
    obj._start_warm(obj.prewarm_prompts)
    await obj._warm
    assert [call.args[0] for call in obj.router.prewarm.await_args_list] == [
        "fast",
        "deep",
        "deep_light",
    ]
    assert [record["pass"] for record in obj.records] == ["fast", "deep", "deep_light"]


async def test_superseded_before_initial_warm_starts_has_no_side_effects() -> None:
    obj = complete_host()
    obj.warm_models()
    old = obj._warm
    await newer_task(obj)
    await old
    obj.load_sections.assert_not_awaited()
    obj.router.warm.assert_not_awaited()
    obj.prewarm_prompts.assert_not_awaited()


async def test_mode_change_after_pack_before_sections_return_does_not_prewarm_old_prefix() -> None:
    obj = complete_host()
    obj.manager = SimpleNamespace(
        employer_pack=AsyncMock(return_value=SimpleNamespace(label="current"))
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def sections() -> None:
        entered.set()
        await release.wait()

    obj.load_sections = sections
    obj._start_warm(lambda: obj._mode_changed("standup"))
    old = obj._warm
    await entered.wait()
    await newer_task(obj)
    release.set()
    await old
    assert obj.applied == ["current"]
    obj.prewarm_prompts.assert_not_awaited()
