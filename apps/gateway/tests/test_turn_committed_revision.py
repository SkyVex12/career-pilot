"""Committed STT words can revise a candidate before the separate flush acknowledgement."""

import asyncio
from collections.abc import Awaitable
from typing import Any
from uuid import UUID, uuid4

import pytest

from copilot_gateway.stt import TimedResult
from copilot_gateway.turn import Detection, TurnDetector

PARTIAL = "Alex, what did you finish yesterday, and are there"
FULL = "Alex, what did you finish yesterday and are there any blockers?"
ANSWER = Detection("answer", "standup_status")


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, asyncio.Future[Detection]]] = []
        self.requests: asyncio.Queue[tuple[str, asyncio.Future[Detection]]] = asyncio.Queue()
        self.held: list[tuple[UUID, str]] = []
        self.shown: list[tuple[UUID, str]] = []
        self.shown_events: asyncio.Queue[tuple[UUID, str]] = asyncio.Queue()
        self.discarded: list[UUID] = []
        self.cancelled: list[UUID] = []
        self.confirmed: list[tuple[UUID, str, int | None]] = []
        self.confirmed_event = asyncio.Event()
        self.matches: list[asyncio.Task[object]] = []
        self.pending_matches = False

    def detect(self, utterance: str) -> Awaitable[Detection]:
        call = (utterance, asyncio.get_running_loop().create_future())
        self.calls.append(call)
        self.requests.put_nowait(call)
        return call[1]

    def start_match(self, utterance: str) -> asyncio.Task[object] | None:
        if not self.pending_matches:
            return None

        async def match() -> object:
            await asyncio.Event().wait()
            return None

        task = asyncio.create_task(match())
        self.matches.append(task)
        return task

    def start_held(self, text: str, t0: float, match: asyncio.Task[object] | None) -> UUID:
        qid = uuid4()
        self.held.append((qid, text))
        return qid

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        self.shown.append((qid, text))
        self.shown_events.put_nowait((qid, text))

    def begin_question(
        self,
        text: str,
        detection: Detection,
        *,
        speculative: bool,
        t0: float,
        match: asyncio.Task[object] | None,
    ) -> UUID:
        qid = uuid4()
        self.release_question(qid, text, detection, speculative=speculative)
        return qid

    def confirm_question(
        self, qid: UUID, text: str, turn_end_ms: int, final_ms: int | None
    ) -> None:
        self.confirmed.append((qid, text, final_ms))
        self.confirmed_event.set()

    def cancel_question(self, qid: UUID) -> None:
        self.cancelled.append(qid)

    def discard_question(self, qid: UUID) -> None:
        self.discarded.append(qid)

    def retime_held(self, qid: UUID, t0: float) -> None:
        pass

    def retype_question(self, qid: UUID, detection: Detection) -> None:
        pass


class Harness:
    def __init__(self, **options: Any) -> None:
        self.host = Host()
        config: dict[str, Any] = {
            "silence_timer_ms": 0,
            "speculative_start_ms": 0,
            "final_wait_max_ms": 1000,
            "committed_final_recheck": True,
            "group_call": lambda: True,
            "hold_any_min_words": 3,
            "strict_extensions": True,
        }
        config.update(options)
        self.turns = TurnDetector(self.host, **config)

    def result(self, text: str, *, final: bool = False, flushed: bool = False) -> None:
        self.turns.on_result(TimedResult(text, final, flushed, 0, 100, "fixture"))

    def start(self, text: str = PARTIAL) -> None:
        self.turns.on_speech_start()
        self.result(text)
        self.turns.on_speech_end()

    async def request(self, text: str) -> asyncio.Future[Detection]:
        actual, future = await asyncio.wait_for(self.host.requests.get(), 1)
        assert actual == text
        return future

    async def shown(self, text: str) -> UUID:
        qid, actual = await asyncio.wait_for(self.host.shown_events.get(), 1)
        assert actual == text
        return qid

    async def finish(self) -> None:
        self.result("", final=True, flushed=True)
        task = self.turns.task
        assert task is not None
        await asyncio.wait_for(task, 1)

    async def close(self) -> None:
        tasks = [task for task in (self.turns.task, *self.turns._detached) if task is not None]
        self.turns.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self.host.matches, return_exceptions=True)


@pytest.mark.parametrize("enabled", [False, True])
async def test_changed_committed_final_replaces_pending_partial_before_flush(enabled: bool) -> None:
    env = Harness(committed_final_recheck=enabled)
    try:
        env.start()
        old = await env.request(PARTIAL)
        env.result(FULL, final=True)
        if not enabled:
            # The control reproduces the live failure: committed words are ignored until flush.
            old.set_result(ANSWER)
            await env.shown(PARTIAL)
            assert len(env.host.calls) == 1
            env.result("", final=True, flushed=True)
        new = await env.request(FULL)
        assert old.cancelled() if enabled else old.done()
        if enabled:
            assert env.host.shown == []
            assert env.host.held[0][0] in env.host.discarded
            turn = env.turns.turn
            assert turn is not None
            assert not turn.flushed.is_set()
            assert turn.flushed_at is None
        new.set_result(ANSWER)
        await env.shown(FULL)
        if enabled:
            assert env.host.confirmed == []  # Segment-final is not turn completion.
        await env.finish()
        assert [text for _, text, _ in env.host.confirmed] == [FULL]
        assert len(env.host.calls) == 2
    finally:
        await env.close()


async def test_default_flag_retains_flush_only_revision_behavior() -> None:
    host = Host()
    turns = TurnDetector(host, silence_timer_ms=0, speculative_start_ms=0, final_wait_max_ms=100)
    assert not turns.committed_final_recheck


async def test_final_before_turn_task_starts_needs_no_redundant_call() -> None:
    env = Harness()
    try:
        env.start()
        env.result(FULL, final=True)  # No event-loop yield after the speech-end callback.
        future = await env.request(FULL)
        future.set_result(ANSWER)
        await env.shown(FULL)
        await env.finish()
        assert len(env.host.calls) == len(env.host.held) == 1
    finally:
        await env.close()


async def test_unchanged_final_and_empty_flush_keep_pending_candidate() -> None:
    env = Harness()
    try:
        env.start(FULL)
        future = await env.request(FULL)
        env.result(FULL, final=True)
        future.set_result(ANSWER)
        await env.shown(FULL)
        assert env.host.confirmed == []
        await env.finish()
        assert len(env.host.calls) == len(env.host.held) == 1
        assert not env.host.cancelled
        assert not env.host.discarded
    finally:
        await env.close()


async def test_revision_after_release_rechecks_before_delayed_flush() -> None:
    env = Harness()
    try:
        env.start()
        old = await env.request(PARTIAL)
        old.set_result(ANSWER)
        old_qid = await env.shown(PARTIAL)
        env.result(FULL, final=True)
        new = await env.request(FULL)
        assert old_qid in env.host.cancelled
        assert env.host.confirmed == []
        new.set_result(ANSWER)
        new_qid = await env.shown(FULL)
        assert new_qid != old_qid
        await env.finish()
        assert [qid for qid, _, _ in env.host.confirmed] == [new_qid]
    finally:
        await env.close()


async def test_committed_snapshot_does_not_absorb_a_later_interim_tail() -> None:
    env = Harness()
    try:
        env.start()
        await env.request(PARTIAL)
        env.result(FULL, final=True)
        env.result("Also a tentative correction")  # Not committed, so not this revision's input.
        new = await env.request(FULL)
        new.set_result(ANSWER)
        await env.shown(FULL)
        assert len(env.host.calls) == 2
    finally:
        await env.close()


async def test_multiple_committed_parts_cancel_each_obsolete_candidate() -> None:
    env = Harness()
    try:
        env.start()
        first = await env.request(PARTIAL)
        env.result(FULL, final=True)
        second = await env.request(FULL)
        extra = "Please include security review blockers."
        env.result(extra, final=True)
        third = await env.request(FULL + " " + extra)
        assert first.cancelled()
        assert second.cancelled()
        assert env.host.shown == []
        third.set_result(ANSWER)
        await env.shown(FULL + " " + extra)
        await env.finish()
        assert len(env.host.calls) == len(env.host.held) == 3
    finally:
        await env.close()


@pytest.mark.parametrize("commits", [1, 2])
@pytest.mark.parametrize("stop", ["cancel", "resume"])
async def test_cancel_during_replacement_drains_current_detector_and_match(
    commits: int, stop: str
) -> None:
    env = Harness()
    env.host.pending_matches = True
    env.start()
    pending = [await env.request(PARTIAL)]
    text = ""
    for part in [FULL, "Please include security review blockers."][:commits]:
        text = (text + " " + part).strip()
        env.result(part, final=True)
        pending.append(await env.request(text))
    if stop == "resume":
        task = env.turns.task
        env.turns.on_speech_start()
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
    await env.close()
    assert all(future.cancelled() for future in pending)
    assert all(task.done() for task in env.host.matches)
    assert env.host.shown == env.host.confirmed == []
    assert {qid for qid, _ in env.host.held} == set(env.host.discarded)


async def test_resume_cancels_unflushed_turn_without_claiming_segment_completion() -> None:
    env = Harness()
    try:
        env.start()
        await env.request(PARTIAL)
        env.result(FULL, final=True)
        new = await env.request(FULL)
        task = env.turns.task
        env.turns.on_speech_start()
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        assert new.cancelled()
        assert env.turns.detached_turns == 0
        assert env.host.shown == env.host.confirmed == []
        # A segment final while speech continues must not reawaken the canceled turn.
        env.result("I have one more constraint.", final=True)
        await asyncio.sleep(0)
        assert len(env.host.calls) == 2
    finally:
        await env.close()


async def test_mid_speech_final_reconciles_early_candidate_only_at_speech_end() -> None:
    env = Harness(early_interval_ms=1, early_all_modes=True)
    try:
        initial = "What did you finish yesterday?"
        env.turns.on_speech_start()
        env.result(initial)
        old = await env.request(initial)
        # A final without terminal punctuation does not itself start an early request.
        committed = "What did you finish yesterday and what is blocked"
        env.result(committed, final=True)
        assert env.turns.turn is None
        assert len(env.host.calls) == 1
        assert env.host.shown == []
        env.turns.on_speech_end()
        new = await env.request(committed)
        assert old.cancelled()
        new.set_result(ANSWER)
        await env.shown(committed)
        assert env.host.confirmed == []
        await env.finish()
    finally:
        await env.close()


async def test_detached_turn_cannot_take_next_utterances_committed_words() -> None:
    env = Harness()
    try:
        env.start(FULL)
        old = await env.request(FULL)
        env.result(FULL, final=True, flushed=True)
        old_turn = env.turns.turn
        env.turns.on_speech_start()
        assert env.turns.detached_turns == 1
        next_text = "What should we prioritize tomorrow?"
        env.result(next_text, final=True)
        assert old_turn is not None
        assert old_turn.text == FULL
        assert old_turn.committed_text == FULL
        old.set_result(ANSWER)
        await env.shown(FULL)
        await asyncio.wait_for(env.host.confirmed_event.wait(), 1)
        assert env.host.confirmed[0][1] == FULL
        assert len(env.host.calls) == 1
    finally:
        await env.close()


@pytest.mark.parametrize("rest_arrives", [False, True])
async def test_prefix_segment_final_does_not_truncate_candidate_or_complete_turn(
    rest_arrives: bool,
) -> None:
    env = Harness()
    try:
        env.start(FULL)
        future = await env.request(FULL)
        prefix = "Alex, what did you finish yesterday"
        env.result(prefix, final=True)
        future.set_result(ANSWER)
        await env.shown(FULL)
        assert len(env.host.calls) == 1
        assert env.host.confirmed == []
        turn = env.turns.turn
        assert turn is not None
        assert not turn.flushed.is_set()
        assert turn.flushed_at is None
        if rest_arrives:
            # Committing the rest recovers the whole request without another call.
            env.result("and are there any blockers?", final=True)
        else:
            # Only the actual flush may establish that the shorter final is complete.
            env.result("", final=True, flushed=True)
            revised = await env.request(prefix)
            revised.set_result(ANSWER)
            await env.shown(prefix)
        await env.finish()
        assert len(env.host.calls) == (1 if rest_arrives else 2)
        assert env.host.confirmed[0][1] == (FULL if rest_arrives else prefix)
    finally:
        await env.close()
