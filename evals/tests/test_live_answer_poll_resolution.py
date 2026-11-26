import asyncio
import time
from types import SimpleNamespace

import pytest
from copilot_evals import live_answer_session_replay as candidate


def session(task=None, questions=None):
    return SimpleNamespace(
        turns=SimpleNamespace(_detached=[], task=task),
        answers=SimpleNamespace(questions=questions or {}),
    )


async def test_settle_waits_for_work_and_full_quiet_period(monkeypatch):
    raw_sleep = asyncio.sleep
    delays = []
    ended = []

    async def work():
        await raw_sleep(0.08)
        ended.append(time.monotonic())

    task = asyncio.create_task(work())

    async def counted(delay):
        delays.append(delay)
        await raw_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", counted)
    await candidate.settle(session(task), 1)
    assert ended
    assert time.monotonic() - ended[0] > 0.1
    assert len(delays) < 50
    assert all(x >= max(0.01, 2 * time.get_clock_info("monotonic").resolution) for x in delays)


async def test_settle_observes_detached_and_answer_tasks():
    marker = asyncio.Event()
    task = asyncio.create_task(marker.wait())
    state = session(questions={"q": SimpleNamespace(tasks={"fast": task})})
    with pytest.raises(TimeoutError):
        await candidate.settle(state, 0.06)
    assert not task.done()
    marker.set()
    await task
    await candidate.settle(state, 1)


async def test_wait_started_retains_exact_question_and_requested_pass():
    state = session(questions={"q": SimpleNamespace(text="Question?", started_passes={"deep"})})
    with pytest.raises(TimeoutError):
        await candidate.wait_started(state, {"text": "Question?"}, 0.06)
    await candidate.wait_started(state, {"text": "Question?", "pass": "deep"}, 1)


async def test_busy_ready_queue_cannot_create_unbounded_poll_density(monkeypatch):
    raw_sleep = asyncio.sleep
    polls = []

    async def counted(delay):
        polls.append(delay)
        await raw_sleep(delay)

    state = session(questions={"q": SimpleNamespace(text="Question?", started_passes=set())})
    keep_running = True

    async def busy_ready():
        while keep_running:
            await raw_sleep(0)

    load = asyncio.create_task(busy_ready())
    monkeypatch.setattr(asyncio, "sleep", counted)
    started = time.perf_counter()
    try:
        with pytest.raises(TimeoutError):
            await candidate.wait_started(state, {"text": "Question?"}, 0.15)
    finally:
        keep_running = False
        await load
    elapsed = time.perf_counter() - started
    assert elapsed < 1
    assert 1 <= len(polls) < 40


async def test_cancelled_wait_exits_without_mutating_session():
    state = session(questions={"q": SimpleNamespace(text="Question?", started_passes=set())})
    task = asyncio.create_task(candidate.wait_started(state, {"text": "Question?"}, 5))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not state.answers.questions["q"].started_passes
