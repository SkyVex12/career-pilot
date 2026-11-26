"""Hidden stable drafts and final-matched release; both remain opt-in experiments."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_gateway.stt import TimedResult
from copilot_gateway.turn import Detection, TurnDetector, TurnHost

QUESTION = "How did you build the rate limiter"


def fixture(**options):
    held, shown, discarded, cancelled, detected = [], [], [], [], []
    started, released = asyncio.Event(), asyncio.Event()
    decision = {"value": "answer"}

    def detect(text):
        detected.append(text)
        future = asyncio.get_running_loop().create_future()
        future.set_result(Detection(decision["value"], "technical"))
        return future

    def hold(text, t0, match):
        qid = uuid4()
        held.append((qid, text))
        started.set()
        return qid

    def release(qid, text, detection, *, speculative):
        shown.append((qid, text, time.monotonic()))
        released.set()

    host = SimpleNamespace(
        detect=detect,
        start_match=lambda text: None,
        start_held=hold,
        release_question=release,
        discard_question=discarded.append,
        cancel_question=cancelled.append,
        retime_held=lambda *args: None,
        confirm_question=lambda *args: None,
        retype_question=lambda *args: None,
        begin_question=lambda *args, **kwargs: uuid4(),
    )
    config: dict[str, Any] = dict(
        silence_timer_ms=1200,
        speculative_start_ms=1000,
        final_wait_max_ms=100,
        early_interval_ms=20,
        early_all_modes=True,
        early_stable_partial_ms=40,
        early_final_release=True,
        keep_added_words=2,
        strict_extensions=True,
    )
    config.update(options)
    turns = TurnDetector(cast(TurnHost, host), **config)
    return SimpleNamespace(
        turns=turns,
        held=held,
        shown=shown,
        discarded=discarded,
        cancelled=cancelled,
        detected=detected,
        started=started,
        released=released,
        decision=decision,
    )


def interim(env, text=QUESTION):
    env.turns.on_result(TimedResult(text, False, False, 0, 100, "fixture"))


def final(env, text=QUESTION):
    env.turns.on_result(TimedResult(text, True, True, 0, 100, "fixture"))


async def cleanup(env):
    task = env.turns.task
    env.turns.cancel()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def test_unchanged_partial_starts_hidden_once_and_keeps_duplicate_timer():
    env = fixture()
    try:
        env.turns.on_speech_start()
        interim(env)
        timer = env.turns._stable_timer
        interim(env)
        assert env.turns._stable_timer is timer
        await asyncio.wait_for(env.started.wait(), 1)
        assert env.shown == []
        assert env.turns.stable_early_starts == 1
        interim(env)
        await asyncio.sleep(0.08)
        assert len(env.held) == 1
    finally:
        await cleanup(env)


@pytest.mark.parametrize(
    "options",
    [
        {"early_stable_partial_ms": None},
        {"early_interval_ms": None},
        {"early_all_modes": False},
    ],
)
async def test_stable_draft_respects_existing_enablement(options):
    env = fixture(**options)
    try:
        env.turns.on_speech_start()
        interim(env)
        await asyncio.sleep(0.08)
        assert env.held == []
    finally:
        await cleanup(env)


@pytest.mark.parametrize("stop", ["cancel", "reset", "language"])
async def test_pending_timer_cannot_fire_after_stop(stop):
    env = fixture()
    env.turns.on_speech_start()
    interim(env)
    if stop == "language":
        await env.turns.change_language("es")
    else:
        getattr(env.turns, stop)()
    await asyncio.sleep(0.08)
    assert env.held == []
    await cleanup(env)


async def test_revision_replaces_pending_draft_and_non_question_cancels_it():
    env = fixture()
    try:
        env.turns.on_speech_start()
        interim(env)
        old_timer = env.turns._stable_timer
        revised = QUESTION + " without Redis"
        interim(env, revised)
        assert old_timer is not None
        assert old_timer.cancelled()
        await asyncio.wait_for(env.started.wait(), 1)
        assert [text for _, text in env.held] == [revised]
        interim(env, "Thanks for joining us today")
        assert env.turns._stable_timer is None
    finally:
        await cleanup(env)


@pytest.mark.parametrize("punctuation", ["", "?"])
async def test_positive_adopted_draft_releases_only_after_matching_final(punctuation):
    env = fixture()
    try:
        env.turns.on_speech_start()
        interim(env)
        await asyncio.wait_for(env.started.wait(), 1)
        env.turns.on_speech_end()
        await asyncio.sleep(0.04)
        assert env.shown == []
        final(env, QUESTION + punctuation)
        await asyncio.wait_for(env.released.wait(), 0.5)
        assert env.shown[0][0] == env.held[0][0]
        assert env.turns.early_final_releases >= 1
        if punctuation:
            # Punctuation that changes detector semantics still receives a new decision.
            assert env.detected[-1] == QUESTION + punctuation
        env.turns.on_speech_start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert env.shown[0][0] in env.cancelled
    finally:
        await cleanup(env)


@pytest.mark.parametrize("case", ["disabled", "changed", "missing_final", "negative"])
async def test_unqualified_drafts_cannot_bypass_display_floor(case):
    env = fixture(early_final_release=case != "disabled")
    try:
        env.turns.on_speech_start()
        interim(env)
        if case == "negative":
            env.decision["value"] = "none"
        await asyncio.wait_for(env.started.wait(), 1)
        env.turns.on_speech_end()
        await asyncio.sleep(0)
        if case != "missing_final":
            final(env, QUESTION + (" without Redis" if case == "changed" else ""))
        await asyncio.sleep(0.12)
        assert env.shown == []
        if case == "changed":
            assert env.held[0][0] in env.discarded
            assert env.held[-1][1].endswith("without Redis")
        assert env.turns.early_final_releases == 0
    finally:
        await cleanup(env)
