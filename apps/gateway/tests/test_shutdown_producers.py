"""Terminal speech/summary races; all inputs and providers are authored locally."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from copilot_gateway.conversation import Budget, Conversation, Line
from copilot_gateway.session import Session
from copilot_gateway.stt import TimedResult

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(("ending", "lost"), [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("callback", ["_on_stt", "_on_me_stt", "_on_stt_status", "_accept_me"])
async def test_terminal_speech_callbacks_do_not_create_retained_writes(
    ending: bool, lost: bool, callback: str
) -> None:
    # No other fields: reaching any state mutation is a regression.
    host = cast(Session, SimpleNamespace(ending=ending, lost=lost))
    result = TimedResult("A late final.", True, True, 10, 20, "offline")
    if callback == "_on_stt_status":
        Session._on_stt_status(host, "degraded", "late transport status")
    elif callback == "_accept_me":
        Session._accept_me(host, 3, Line("me", 10, 20, "A late final."))
    else:
        await getattr(Session, callback)(host, result)


@pytest.mark.parametrize("echo", [False, True])
async def test_settlement_crossing_shutdown_never_emits_echo_or_accept(echo: bool) -> None:
    line = Line("me", 10, 20, "Shared words in the microphone.")
    heard = [line.text] if echo else ["Unrelated conversation"]
    host = cast(
        Any,
        SimpleNamespace(
            ending=False,
            lost=False,
            settings=SimpleNamespace(echo_wait_ms=1000, echo_min_overlap=0.8),
            conv=SimpleNamespace(lines=[], them_text_between=lambda *_: heard),
            them_open=0,
            turns=SimpleNamespace(interim="still listening"),
            _echo=Mock(),
            _accept_me=Mock(),
        ),
    )
    task = asyncio.create_task(Session._settle_me(host, 3, line))
    await asyncio.sleep(0)
    assert not task.done()
    host.ending = True
    host.them_open = None
    host.turns.interim = ""
    await asyncio.wait_for(task, 0.5)
    host._echo.assert_not_called()
    host._accept_me.assert_not_called()


@pytest.mark.parametrize("echo", [False, True])
async def test_active_settlement_keeps_existing_echo_classification(echo: bool) -> None:
    line = Line("me", 10, 20, "Shared words in the microphone.")
    host = cast(
        Any,
        SimpleNamespace(
            ending=False,
            lost=False,
            settings=SimpleNamespace(echo_wait_ms=0, echo_min_overlap=0.8),
            conv=SimpleNamespace(them_text_between=lambda *_: [line.text] if echo else []),
            turns=SimpleNamespace(interim=""),
            _echo=Mock(),
            _accept_me=Mock(),
        ),
    )
    await Session._settle_me(host, 3, line)
    assert host._echo.call_count == int(echo)
    assert host._accept_me.call_count == int(not echo)


def conversation(summarize: Any, changed: Any = None) -> Conversation:
    conv = Conversation(
        "Alex", Budget(total=200, summary=50, answers=50, chunk=40), summarize, on_change=changed
    )
    conv.lines = [Line("them", i * 1000, i * 1000 + 900, "facts " * 90) for i in range(3)]
    conv.summary = "Previously retained summary."
    return conv


async def test_terminal_close_suppresses_late_fold_state_but_keeps_returned_usage() -> None:
    started = asyncio.Event()
    retained: list[str] = []

    async def summarize(previous: str, lines: str) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A provider completing during cancellation can still return billable usage.
            retained.append("returned usage")
            return "Late summary result."
        raise AssertionError("The authored provider only completes during cancellation")

    changed = Mock()
    conv = conversation(summarize, changed)
    original = list(conv.lines)
    conv._maybe_fold()
    task = conv._task
    assert task is not None
    await started.wait()
    conv.close()
    await asyncio.wait_for(task, 0.5)
    assert retained == ["returned usage"]
    assert conv.lines == original
    assert conv.summary == "Previously retained summary."
    assert not conv.folding
    changed.assert_not_called()
    conv._maybe_fold()
    assert not conv.folding


async def test_terminal_close_without_pending_fold_prevents_new_summary() -> None:
    calls = Mock()

    async def summarize(previous: str, lines: str) -> str:
        calls()
        return "Summary."

    conv = conversation(summarize)
    conv.close()
    conv._maybe_fold()
    await asyncio.sleep(0)
    assert not conv.folding
    calls.assert_not_called()


async def test_language_cancel_fold_remains_reusable() -> None:
    started = asyncio.Event()
    calls = 0

    async def summarize(previous: str, lines: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return "New-language summary."

    conv = conversation(summarize)
    original = list(conv.lines)
    conv._maybe_fold()
    await started.wait()
    await conv.cancel_fold()
    assert conv.lines == original
    assert conv.summary == "Previously retained summary."
    conv._maybe_fold()
    await asyncio.wait_for(conv.wait_folded(), 0.5)
    assert calls >= 2
    assert conv.summary == "New-language summary."


async def test_summary_completed_before_close_keeps_retained_state() -> None:
    retained: list[str] = []

    async def summarize(previous: str, lines: str) -> str:
        retained.append("returned usage")
        return "Completed before shutdown."

    changed = Mock()
    conv = conversation(summarize, changed)
    conv._maybe_fold()
    await conv.wait_folded()
    before = conv.to_state()
    conv.close()
    assert conv.to_state() == before
    assert retained
    assert changed.called
