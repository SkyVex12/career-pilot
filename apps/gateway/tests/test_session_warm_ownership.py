"""Authored lifecycle regressions; providers and retained data stores are stubbed."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMUsage
from copilot_gateway.session import Session
from copilot_gateway.store import RetainedLogOverflowError
from copilot_protocol import models

pytestmark = pytest.mark.asyncio


class StopBeforeDrain(Exception):
    pass


def host() -> Any:
    obj = cast(Any, object.__new__(Session))
    obj.ending = False
    obj.lost = False
    obj._retention_failed = False
    obj._warm = None
    obj._warm_tasks = set()
    obj._language_lock = asyncio.Lock()
    obj._language_changes = set()
    obj._settling = set()
    obj._forced = set()
    obj._forced_detections = {}
    obj._named_detections = {}
    obj.info = SimpleNamespace(mode="interview")
    obj._build_prompt_ctx = lambda: None
    obj.writer = SimpleNamespace(set_state=lambda **_: None)
    obj.log_entry = lambda _: None
    return obj


@pytest.mark.parametrize("terminal", ["ending", "lost", "_retention_failed"])
@pytest.mark.parametrize("kind", ["message", "audio"])
async def test_waiting_ingress_rechecks_terminal_after_language_lock(
    terminal: str,
    kind: str,
) -> None:
    obj = host()
    accepted: list[str] = []
    obj.writer.set_state = lambda **_: accepted.append("state")
    obj.log_entry = lambda _: accepted.append("log")
    obj._mode_changed = AsyncMock()
    obj._on_audio = AsyncMock()
    await obj._language_lock.acquire()
    if kind == "message":
        task = asyncio.create_task(obj.handle(models.ModeSet(type="mode.set", mode="meeting")))
    else:
        task = asyncio.create_task(obj.on_audio(b"authored-frame"))
    await asyncio.sleep(0)
    assert not task.done()
    setattr(obj, terminal, True)
    obj._language_lock.release()
    await task
    assert accepted == []
    assert obj.info.mode == "interview"
    assert obj._warm is None
    obj._mode_changed.assert_not_awaited()
    obj._on_audio.assert_not_awaited()


@pytest.mark.parametrize("kind", ["message", "audio"])
async def test_waiting_ingress_is_accepted_when_session_stays_live(kind: str) -> None:
    obj = host()
    obj._mode_changed = AsyncMock()
    obj._on_audio = AsyncMock()
    await obj._language_lock.acquire()
    if kind == "message":
        task = asyncio.create_task(obj.handle(models.ModeSet(type="mode.set", mode="meeting")))
    else:
        task = asyncio.create_task(obj.on_audio(b"authored-frame"))
    await asyncio.sleep(0)
    obj._language_lock.release()
    await task
    if kind == "message":
        await obj._warm
        assert obj.info.mode == "meeting"
        obj._mode_changed.assert_awaited_once_with("meeting")
    else:
        obj._on_audio.assert_awaited_once_with(b"authored-frame")


async def two_mode_tasks(obj: Any) -> list[asyncio.Task[None]]:
    entered = {mode: asyncio.Event() for mode in ("standup", "meeting")}

    async def mode_changed(mode: str) -> None:
        entered[mode].set()
        await asyncio.Event().wait()

    obj._mode_changed = mode_changed
    tasks: list[asyncio.Task[None]] = []
    for mode in ("standup", "meeting"):
        await obj.handle(models.ModeSet(type="mode.set", mode=mode))
        tasks.append(obj._warm)
        await entered[mode].wait()
    assert len(set(tasks)) == 2
    return tasks


async def test_all_overlapping_mode_tasks_are_owned_until_done() -> None:
    obj = host()
    tasks = await two_mode_tasks(obj)
    try:
        assert obj._warm_tasks == set(tasks)
        assert obj._warm is tasks[-1]
        obj._cancel_warm()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert all(task.cancelled() for task in tasks)
        assert obj._warm_tasks == set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_initial_warm_and_mode_warm_share_ownership() -> None:
    obj = host()
    first_entered = asyncio.Event()
    mode_entered = asyncio.Event()

    async def sections() -> None:
        first_entered.set()
        await asyncio.Event().wait()

    async def changed(_: str) -> None:
        mode_entered.set()
        await asyncio.Event().wait()

    obj.load_sections = sections
    obj._mode_changed = changed
    obj.warm_models()
    first = obj._warm
    await first_entered.wait()
    await obj.handle(models.ModeSet(type="mode.set", mode="meeting"))
    second = obj._warm
    await mode_entered.wait()
    try:
        assert obj._warm_tasks == {first, second}
        obj._cancel_warm()
        await asyncio.gather(first, second, return_exceptions=True)
        assert first.cancelled()
        assert second.cancelled()
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.parametrize("terminal", ["ending", "lost", "_retention_failed"])
async def test_terminal_session_does_not_construct_warm_coroutine(terminal: str) -> None:
    obj = host()
    setattr(obj, terminal, True)
    constructed: list[bool] = []

    def factory() -> Any:
        constructed.append(True)
        raise AssertionError("terminal session must not invoke the coroutine factory")

    obj._start_warm(factory)
    obj.warm_models()
    assert constructed == []
    assert obj._warm is None
    assert not obj._warm_tasks


@pytest.mark.parametrize("path", ["end", "abandon", "lost", "retention", "stop"])
async def test_every_stop_path_cancels_all_warm_tasks_before_its_first_await(path: str) -> None:
    obj = host()
    tasks = await two_mode_tasks(obj)
    obj.sid = "authored-session"
    obj.ws = None
    obj.usage_meter = SimpleNamespace(stop=lambda **_: None)
    obj.turns = SimpleNamespace(cancel=lambda: None)
    obj.answers = SimpleNamespace(
        shutdown=AsyncMock(side_effect=StopBeforeDrain), cancel_all=lambda: None
    )
    obj.manager = SimpleNamespace(forget=lambda _: None)
    obj.seatdesk = SimpleNamespace(stop=AsyncMock(side_effect=StopBeforeDrain))
    obj._stop_lost = AsyncMock()
    obj.error = lambda *_args, **_kwargs: None
    obj.request_end = lambda *_args: None
    try:
        if path == "end":
            with pytest.raises(StopBeforeDrain):
                await obj.end("authored")
        elif path == "abandon":
            with pytest.raises(StopBeforeDrain):
                await obj.abandon(1011, "authored")
        elif path == "lost":
            obj.on_lease_lost()
            await obj.end_task
        elif path == "retention":
            obj._on_retention_overflow(RetainedLogOverflowError("authored"))
        else:
            with pytest.raises(StopBeforeDrain):
                await obj._stop()
        assert all(task.cancelling() == 1 for task in tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_language_change_cancels_and_joins_all_old_warm_tasks() -> None:
    obj = host()
    tasks = await two_mode_tasks(obj)
    obj.spoken_language = "en"
    obj.stt = obj.stt_me = None
    obj._language_generation = 0
    obj._stt_tasks = []
    obj.turns = SimpleNamespace(change_language=AsyncMock())
    obj.answers = SimpleNamespace(change_language=AsyncMock())
    obj.conv = SimpleNamespace(cancel_fold=AsyncMock())
    try:
        await obj._replace_language_streams("fr")
        assert all(task.cancelled() for task in tasks)
        assert not obj._warm_tasks
        obj.answers.change_language.assert_awaited_once()
        obj.conv.cancel_fold.assert_awaited_once()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_repeated_stop_does_not_recancel_returned_prewarm_usage() -> None:
    obj = host()
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    returned = LLMDone(
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
    records: list[dict[str, Any]] = []

    async def prewarm(*_: Any) -> LLMDone:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Model a provider whose already accepted request returns usage during cleanup.
            cancelling.set()
            await release.wait()
            return returned
        raise AssertionError("the authored provider only completes during cancellation cleanup")

    obj.router = SimpleNamespace(prewarm=prewarm)
    obj._prompt_ctx = prompts.PromptContext("Alex", "interview", None, None)
    obj.settings = SimpleNamespace(prewarm_jobs=["fast"], fast_pass_max_tokens=50)
    obj._cost = lambda _: 0.001
    obj.log_entry = records.append
    obj._start_warm(obj.prewarm_prompts)
    task = obj._warm
    await entered.wait()
    obj.ending = True
    obj._cancel_warm()
    await cancelling.wait()
    obj._cancel_warm()
    assert task.cancelling() == 1
    assert task in obj._warm_tasks
    assert not task.done()
    release.set()
    await task
    await asyncio.sleep(0)
    assert [record["kind"] for record in records] == ["prewarm"]
    assert records[0]["input_tokens"] == 12
    assert records[0]["cost_usd"] == 0.001
    assert not obj._warm_tasks
