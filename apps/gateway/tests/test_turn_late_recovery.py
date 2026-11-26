"""Delayed STT recovery belongs to one silent turn and needs a fresh utterance flush."""

import asyncio
import time
from collections.abc import Awaitable
from typing import Any
from uuid import UUID, uuid4

import pytest

from copilot_gateway.stt import TimedResult
from copilot_gateway.stt.resilient import _Timeline
from copilot_gateway.turn import Detection, TurnDetector

QUESTION = "Tell me about yourself and your engineering experience."
PARTIAL = "Tell me about yourself"


class Host:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.held: list[tuple[UUID, str, float]] = []
        self.shown: list[tuple[UUID, str]] = []
        self.confirmed: list[tuple[UUID, str, int, int | None]] = []
        self.cancelled: list[UUID] = []
        self.discarded: list[UUID] = []
        self.gate: asyncio.Event | None = None
        self.detect_started = asyncio.Event()
        self.detect_cancelled = asyncio.Event()
        self.match: asyncio.Task[object] | None = None
        self.matches: list[asyncio.Task[object]] = []

    async def detect(self, utterance: str) -> Detection:
        self.calls.append(utterance)
        self.detect_started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            return Detection("answer", "behavioral")
        except asyncio.CancelledError:
            self.detect_cancelled.set()
            raise

    def start_match(self, utterance: str) -> asyncio.Task[object] | None:
        if self.gate is None:
            return None

        async def pending() -> object:
            await asyncio.Event().wait()
            return None

        self.match = asyncio.create_task(pending())
        self.matches.append(self.match)
        return self.match

    def start_held(self, text: str, t0: float, match: asyncio.Task[object] | None) -> UUID:
        qid = uuid4()
        self.held.append((qid, text, t0))
        return qid

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        self.shown.append((qid, text))

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
        self.confirmed.append((qid, text, turn_end_ms, final_ms))

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
            "final_wait_max_ms": 5,
            "late_first_text_recovery_ms": 1000,
            "group_call": lambda: True,
            "hold_any_min_words": 3,
        }
        config.update(options)
        self.turns = TurnDetector(self.host, **config)

    def result(
        self,
        text: str,
        *,
        final: bool = False,
        flushed: bool = False,
        start: int = 100,
        end: int = 1000,
    ) -> None:
        self.turns.on_result(TimedResult(text, final, flushed, start, end, "replayed"))

    def start_empty(self, start: int | None = 100, end: int | None = 1100) -> None:
        self.turns.on_speech_start(capture_ms=start)
        self.turns.on_speech_end(capture_ms=end)

    async def drain(self) -> None:
        task = self.turns.task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), 2)

    async def close(self) -> None:
        tasks: list[Awaitable[object]] = [
            task for task in (self.turns.task, *self.turns._detached) if task is not None
        ]
        self.turns.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for match in self.host.matches:
            match.cancel()
        await asyncio.gather(*self.host.matches, return_exceptions=True)


async def test_first_transcript_after_real_1500ms_wait_only_recovers_when_enabled() -> None:
    old = Harness(final_wait_max_ms=1500, late_first_text_recovery_ms=None)
    new = Harness(final_wait_max_ms=1500, late_first_text_recovery_ms=10000)
    try:
        old.start_empty()
        new.start_empty()
        original = new.turns.turn
        assert original is not None
        original_t0 = original.t0
        await asyncio.gather(old.drain(), new.drain())
        assert time.monotonic() - original_t0 >= 1.5
        assert old.host.calls == new.host.calls == []
        for env in (old, new):
            env.result(QUESTION, final=True, flushed=True)
        await new.drain()
        assert old.host.calls == []
        assert new.host.calls == [QUESTION]
        assert new.turns.turn is original
        assert original.t0 == original_t0 == new.host.held[0][2]
        assert new.host.confirmed[0][2] >= 1500
        assert new.turns.late_first_text_recoveries == 1
    finally:
        await old.close()
        await new.close()


async def test_late_partial_and_segment_final_wait_for_fresh_flush_without_stale_question() -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.result("", final=True, flushed=True)  # Empty acknowledgement predates any words.
        env.result(PARTIAL)
        await asyncio.sleep(0)
        assert not env.host.calls
        turn = env.turns.turn
        assert turn is not None
        assert not turn.flushed.is_set()
        env.result(QUESTION, final=True)
        await asyncio.sleep(0)
        assert not env.host.calls
        assert not env.host.confirmed
        env.result("", final=True, flushed=True)
        await env.drain()
        assert env.host.calls == [QUESTION]
        assert [text for _, text in env.host.shown] == [QUESTION]
        assert [text for _, text, _, _ in env.host.confirmed] == [QUESTION]
        assert env.turns.late_first_text_recoveries == 1
    finally:
        await env.close()


async def test_no_flush_expires_with_no_detector_or_answer_and_cannot_rearm() -> None:
    env = Harness(late_first_text_recovery_ms=60)
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True)
        await env.drain()
        assert not env.host.calls
        assert not env.turns.utterance()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.sleep(0)
        assert not env.host.calls
        assert env.turns.late_first_text_recoveries == 1
    finally:
        await env.close()


@pytest.mark.parametrize(
    ("start", "end"), [(0, 90), (90, 200), (1000, 1200), (1100, 1200), (100, 100), (300, 200)]
)
async def test_disjoint_or_ambiguous_capture_span_cannot_seed_recovery(
    start: int, end: int
) -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.result("Old or mixed utterance", final=True, flushed=True, start=start, end=end)
        assert not env.turns.utterance()
        assert env.turns.late_first_text_recoveries == 0
        env.result(QUESTION, final=True, flushed=True, start=100, end=1100)
        await env.drain()
        assert env.host.calls == [QUESTION]
    finally:
        await env.close()


@pytest.mark.parametrize(("start", "end"), [(None, 1100), (100, None), (1100, 100), (100, 100)])
async def test_missing_or_invalid_capture_anchors_do_not_enable_recovery(
    start: int | None, end: int | None
) -> None:
    env = Harness()
    try:
        env.start_empty(start, end)
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.sleep(0)
        assert not env.host.calls
        assert env.turns.late_first_text_recoveries == 0
    finally:
        await env.close()


async def test_late_result_outside_horizon_does_not_rearm_or_mutate_text() -> None:
    env = Harness(late_first_text_recovery_ms=10)
    try:
        env.start_empty()
        await env.drain()
        await asyncio.sleep(0.02)
        env.result(QUESTION, final=True, flushed=True)
        assert env.turns.late_first_text_recoveries == 0
        assert not env.turns.utterance()
        assert not env.host.calls
    finally:
        await env.close()


@pytest.mark.parametrize("after_new_end", [False, True])
async def test_new_speech_retires_old_capture_before_buffer_mutation(after_new_end: bool) -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.turns.on_speech_start(capture_ms=2000)
        if after_new_end:
            env.turns.on_speech_end(capture_ms=3000)
            await env.drain()
        env.result("Old question?", final=True, flushed=True)
        env.result("Ambiguous old and new", final=True, start=900, end=2300)
        assert not env.turns.utterance()
        assert not env.host.calls
        env.result(QUESTION, final=True, flushed=after_new_end, start=2000, end=2900)
        if not after_new_end:
            env.turns.on_speech_end(capture_ms=3000)
            env.result("", final=True, flushed=True, start=2900, end=2900)
        await env.drain()
        assert env.host.calls == [QUESTION]
        assert len(env.host.confirmed) == 1
    finally:
        await env.close()


@pytest.mark.parametrize("action", ["cancel", "language", "resume"])
async def test_pending_recovery_is_owned_and_cancelled_before_flush(action: str) -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL)
        pending = env.turns.task
        assert pending is not None
        await asyncio.sleep(0)
        if action == "cancel":
            env.turns.cancel()
        elif action == "language":
            await env.turns.change_language("es")
        else:
            env.turns.on_speech_start(capture_ms=2000)
        await asyncio.gather(pending, return_exceptions=True)
        env.result(QUESTION, final=True, flushed=True)
        assert pending.cancelled()
        assert not env.host.calls
        assert not env.turns.utterance()
    finally:
        await env.close()


async def test_cancel_after_recovered_detector_started_drains_detector_and_match() -> None:
    env = Harness()
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        pending = env.turns.task
        assert pending is not None
        env.turns.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert env.host.detect_cancelled.is_set()
        assert env.host.match is not None
        assert env.host.match.cancelled()
        assert env.host.discarded == [env.host.held[0][0]]
        assert not env.host.shown
        assert not env.host.confirmed
    finally:
        await env.close()


async def test_duplicate_results_and_flushes_do_not_revive_completed_turn() -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await env.drain()
        for _ in range(3):
            env.result(QUESTION, final=True, flushed=True)
            env.result("", final=True, flushed=True)
        await asyncio.sleep(0)
        assert env.host.calls == [QUESTION]
        assert len(env.host.confirmed) == 1
        assert env.turns.late_first_text_recoveries == 1
    finally:
        await env.close()


@pytest.mark.parametrize("horizon", [None, 1000])
async def test_normal_first_text_route_is_unchanged(horizon: int | None) -> None:
    env = Harness(late_first_text_recovery_ms=horizon)
    try:
        env.turns.on_speech_start(capture_ms=100)
        env.result(QUESTION, final=True)
        env.turns.on_speech_end(capture_ms=1100)
        env.result("", final=True, flushed=True)
        await env.drain()
        assert env.host.calls == [QUESTION]
        assert len(env.host.held) == len(env.host.confirmed) == 1
        assert env.turns.late_first_text_recoveries == 0
    finally:
        await env.close()


async def test_reconnected_timeline_result_uses_original_capture_span() -> None:
    # Provider timestamps restart at zero; replay preserves the original capture clock.
    timeline = _Timeline()
    timeline.add(5000, 500 * 32)
    timeline.add(5500, 500 * 32)
    env = Harness()
    try:
        env.start_empty(5000, 6000)
        await env.drain()
        env.result(
            QUESTION,
            final=True,
            flushed=True,
            start=timeline.to_capture(0),
            end=timeline.to_capture(950),
        )
        await env.drain()
        assert env.host.calls == [QUESTION]
    finally:
        await env.close()


async def test_empty_flush_at_exact_capture_end_completes_owned_recovery() -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True)
        env.result("", final=True, flushed=True, start=1100, end=1100)
        await env.drain()
        assert env.host.calls == [QUESTION]
        assert len(env.host.confirmed) == 1
    finally:
        await env.close()


async def test_recovered_detector_keeps_capture_ownership_until_confirmation() -> None:
    env = Harness()
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        env.result("Stale other question", final=True, flushed=True, start=0, end=50)
        env.result("Future other question", final=True, flushed=True, start=2000, end=3000)
        assert env.turns.utterance() == QUESTION
        env.host.gate.set()
        await env.drain()
        assert env.host.calls == [QUESTION]
        assert [text for _, text, _, _ in env.host.confirmed] == [QUESTION]
        if env.host.match is not None:
            env.host.match.cancel()
    finally:
        await env.close()


@pytest.mark.parametrize("already_rearmed", [False, True])
async def test_user_speech_drop_carry_retires_empty_turn_and_pending_gate(
    already_rearmed: bool,
) -> None:
    env = Harness()
    try:
        env.start_empty()
        await env.drain()
        assert env.turns.carry_until is None
        if already_rearmed:
            env.result(PARTIAL)
            await asyncio.sleep(0)
        task = env.turns.task
        assert task is not None
        env.turns.drop_carry()
        await asyncio.gather(task, return_exceptions=True)
        env.result(QUESTION, final=True, flushed=True)
        assert not env.host.calls
        assert not env.turns.utterance()
        assert env.turns.task is None
    finally:
        await env.close()


@pytest.mark.parametrize("detach_pending", [False, True])
async def test_recovered_turn_does_not_drop_next_speech_results(detach_pending: bool) -> None:
    env = Harness()
    next_question = "What did you build at your previous company?"
    if detach_pending:
        env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        old_task = env.turns.task
        assert old_task is not None
        if not detach_pending:
            await env.drain()
        env.turns.on_speech_start(capture_ms=2000)
        env.result("Stale previous words", final=True, flushed=True)
        env.result(next_question, final=True, start=2000, end=2900)
        assert env.turns.utterance() == next_question
        env.turns.on_speech_end(capture_ms=3000)
        env.result("", final=True, flushed=True, start=3000, end=3000)
        if env.host.gate is not None:
            env.host.gate.set()
        await env.drain()
        await asyncio.wait_for(old_task, 1)
        assert env.host.calls == [QUESTION, next_question]
        assert sorted(text for _, text, _, _ in env.host.confirmed) == sorted(
            [QUESTION, next_question]
        )
        assert env.turns.late_first_text_recoveries == 1
        if env.host.match is not None:
            env.host.match.cancel()
    finally:
        await env.close()


@pytest.mark.parametrize("rep", [0, 1])
async def test_early_replay_flush_then_tail_final_retains_complete_observed_question(
    rep: int,
) -> None:
    # Exact delayed-first4-r1 words/spans, with provider inter-result gaps scaled by 1/20.
    # Existing final_wait_s scales from 1500ms to75ms; it is explicitly a quiet heuristic.
    env = Harness(final_wait_max_ms=75)
    try:
        env.start_empty(440, 3840)  # 3400ms replayed audio, original capture clock.
        await env.drain()
        env.result("Tell me about your", start=440, end=1460)
        if rep == 1:
            await asyncio.sleep(0.066)  # Observed1313ms before early from_finalize.
        split = 1420 if rep == 0 else 1320
        env.result("Tell me about yourself", final=True, flushed=True, start=440, end=440 + split)
        if rep == 0:
            await asyncio.sleep(0.026)  # Observed517ms to tail partial.
        partial = "your engineering" if rep == 0 else "and your engineering"
        env.result(partial, start=440 + split, end=440 + split + 1000)
        await asyncio.sleep(0)
        assert not env.host.calls
        assert not env.host.shown
        await asyncio.sleep(0.036 if rep == 0 else 0.027)  # Observed719ms /533ms.
        tail = "your engineering experience." if rep == 0 else "and your engineering experience."
        env.result(tail, final=True, start=440 + split, end=3700)
        await env.drain()
        full = "Tell me about yourself " + tail
        assert env.host.calls == [full]
        assert [text for _, text, _, _ in env.host.confirmed] == [full]
        assert not env.host.cancelled
        assert env.turns.late_first_text_recoveries == 1
    finally:
        await env.close()


@pytest.mark.parametrize("empty_final", [False, True])
async def test_empty_ack_never_discards_unresolved_recovery_partial(empty_final: bool) -> None:
    env = Harness(final_wait_max_ms=10, late_first_text_recovery_ms=80)
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True)
        env.result("and your engineering")
        env.result("", final=empty_final, flushed=empty_final, start=1100, end=1100)
        await env.drain()
        assert not env.host.calls
        assert not env.host.confirmed
        assert not env.turns.utterance()
    finally:
        await env.close()


async def test_tail_during_recovered_detector_rechecks_full_final_before_release() -> None:
    env = Harness(final_wait_max_ms=20, late_first_text_recovery_ms=500)
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True, end=400)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        env.result("and your engineering", start=400)
        env.result("", final=True, flushed=True, start=1100, end=1100)
        env.host.gate.set()
        await asyncio.sleep(0.025)
        assert not env.host.shown
        assert not env.host.confirmed
        env.result("and your engineering experience.", final=True, start=400)
        await env.drain()
        full = PARTIAL + " and your engineering experience."
        assert env.host.calls == [PARTIAL, full]
        assert [text for _, text in env.host.shown] == [full]
        assert [text for _, text, _, _ in env.host.confirmed] == [full]
        assert env.host.discarded == [env.host.held[0][0]]
        assert env.host.matches[0].cancelled()
    finally:
        await env.close()


async def test_unresolved_tail_during_detector_expires_without_releasing_old_pass() -> None:
    env = Harness(final_wait_max_ms=10, late_first_text_recovery_ms=100)
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True, end=400)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        env.result("and your engineering", start=400)
        env.host.gate.set()
        await env.drain()
        assert env.host.calls == [PARTIAL]
        assert not env.host.shown
        assert not env.host.confirmed
        assert env.host.discarded == [env.host.held[0][0]]
        assert env.host.match is not None
        assert env.host.match.cancelled()
        assert not env.turns.utterance()
    finally:
        await env.close()


async def test_late_final_cannot_extend_original_horizon_to_finish_quiet_interval() -> None:
    env = Harness(final_wait_max_ms=30, late_first_text_recovery_ms=90)
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True, end=400)
        env.result("and your engineering", start=400)
        await asyncio.sleep(0.04)
        env.result("and your engineering experience.", final=True, start=400)
        await env.drain()
        assert not env.host.calls
        assert not env.host.confirmed
    finally:
        await env.close()


async def test_new_speech_cancels_unresolved_recovered_fragment_during_detector() -> None:
    env = Harness(final_wait_max_ms=10)
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True, end=400)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        env.result("and your engineering", start=400)
        old_task = env.turns.task
        assert old_task is not None
        env.turns.on_speech_start(capture_ms=2000)
        await asyncio.gather(old_task, return_exceptions=True)
        assert old_task.cancelled()
        assert env.host.detect_cancelled.is_set()
        assert not env.host.shown
        assert not env.turns.utterance()
        assert env.turns.detached_turns == 0
    finally:
        await env.close()


async def test_owned_final_during_release_wait_rebuilds_request_even_for_kept_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Harness(
        final_wait_max_ms=10,
        speculative_start_ms=100,
        keep_added_words=5,
        late_first_text_recovery_ms=500,
    )
    release_entered = asyncio.Event()
    release_continue = asyncio.Event()
    original_release = env.turns._wait_release

    async def release_barrier(turn: Any) -> None:
        release_entered.set()
        await release_continue.wait()
        await original_release(turn)

    monkeypatch.setattr(env.turns, "_wait_release", release_barrier)
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL, final=True, flushed=True, end=400)
        await asyncio.wait_for(release_entered.wait(), 1)
        assert not env.host.shown
        env.result("and your engineering experience.", final=True, start=400)
        release_continue.set()
        await env.drain()
        full = PARTIAL + " and your engineering experience."
        assert env.host.calls == [PARTIAL, full]
        assert [text for _, text, _ in env.host.held] == [PARTIAL, full]
        assert env.host.discarded == [env.host.held[0][0]]
        assert [text for _, text in env.host.shown] == [full]
        assert [text for _, text, _, _ in env.host.confirmed] == [full]
        assert env.turns.held_kept == 0
    finally:
        await env.close()


async def test_recovered_hanging_detector_is_cancelled_at_original_horizon() -> None:
    env = Harness(final_wait_max_ms=10, late_first_text_recovery_ms=100)
    env.host.gate = asyncio.Event()
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        await asyncio.wait_for(env.host.detect_started.wait(), 1)
        await env.drain()
        assert env.host.detect_cancelled.is_set()
        assert env.host.match is not None
        assert env.host.match.cancelled()
        assert env.host.discarded == [env.host.held[0][0]]
        assert not env.host.shown
        assert not env.host.confirmed
        assert not env.turns.utterance()
    finally:
        await env.close()


@pytest.mark.parametrize("tail_arrives", [False, True])
async def test_segment_prefix_final_does_not_resolve_already_observed_partial_tail(
    tail_arrives: bool,
) -> None:
    env = Harness(final_wait_max_ms=10, late_first_text_recovery_ms=150)
    try:
        env.start_empty()
        await env.drain()
        env.result(PARTIAL + " and your engineering", end=1000)
        env.result(PARTIAL, final=True, flushed=True, end=400)
        await asyncio.sleep(0.02)
        assert not env.host.calls
        assert not env.host.shown
        if tail_arrives:
            env.result("and your engineering experience.", final=True, start=400, end=1000)
        await env.drain()
        expected = [PARTIAL + " and your engineering experience."] if tail_arrives else []
        assert env.host.calls == expected
        assert [text for _, text, _, _ in env.host.confirmed] == expected
    finally:
        await env.close()


async def test_inner_timeout_is_not_misreported_as_recovery_horizon_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Harness()

    async def failing_detector(text: str) -> Detection:
        raise TimeoutError("authored detector failure before recovery deadline")

    monkeypatch.setattr(env.host, "detect", failing_detector)
    try:
        env.start_empty()
        await env.drain()
        env.result(QUESTION, final=True, flushed=True)
        with pytest.raises(TimeoutError, match="authored detector failure"):
            await env.drain()
        assert not env.host.shown
        assert not env.host.confirmed
    finally:
        await env.close()
