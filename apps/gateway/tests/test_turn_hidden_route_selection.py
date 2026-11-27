"""Hidden route selection overlaps speech without changing turn publication rules."""

import asyncio
import gc
from collections.abc import Awaitable
from typing import Any
from uuid import UUID, uuid4

import pytest

from copilot_gateway.stt import TimedResult
from copilot_gateway.turn import Detection, TurnDetector

QUESTION = "How do you preserve exactly once effects when a worker crashes."
EXPERT = Detection("answer", "technical", depth="deep", latency_ms=500)


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, asyncio.Future[Detection]]] = []
        self.requests: asyncio.Queue[tuple[str, asyncio.Future[Detection]]] = asyncio.Queue()
        self.held: list[tuple[UUID, str]] = []
        self.matches: list[asyncio.Task[object]] = []
        self.classified: list[tuple[UUID, str, Detection]] = []
        self.classifications: asyncio.Queue[tuple[UUID, str, Detection]] = asyncio.Queue()
        self.shown: list[tuple[UUID, str]] = []
        self.publications: asyncio.Queue[tuple[UUID, str]] = asyncio.Queue()
        self.confirmed: asyncio.Queue[tuple[UUID, str]] = asyncio.Queue()
        self.discarded: list[UUID] = []
        self.cancelled: list[UUID] = []
        self.retimed: list[UUID] = []
        self.replace_route = True
        self.immediate: Detection | None = None

    def detect(self, utterance: str) -> Awaitable[Detection]:
        future: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
        if self.immediate is not None:
            future.set_result(self.immediate)
        call = utterance, future
        self.calls.append(call)
        self.requests.put_nowait(call)
        return future

    def start_match(self, utterance: str) -> asyncio.Task[object]:
        async def pending() -> object:
            await asyncio.Event().wait()
            return None

        task = asyncio.create_task(pending())
        self.matches.append(task)
        return task

    def start_held(self, text: str, t0: float, match: asyncio.Task[object] | None) -> UUID:
        qid = uuid4()
        self.held.append((qid, text))
        return qid

    def classify_held(self, qid: UUID, text: str, detection: Detection) -> bool:
        assert qid not in self.discarded
        assert qid not in self.cancelled
        assert qid not in {shown[0] for shown in self.shown}
        call = qid, text, detection
        self.classified.append(call)
        self.classifications.put_nowait(call)
        return self.replace_route

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        self.shown.append((qid, text))
        self.publications.put_nowait((qid, text))

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
        self.confirmed.put_nowait((qid, text))

    def discard_question(self, qid: UUID) -> None:
        self.discarded.append(qid)

    def cancel_question(self, qid: UUID) -> None:
        self.cancelled.append(qid)

    def retime_held(self, qid: UUID, t0: float) -> None:
        self.retimed.append(qid)

    def retype_question(self, qid: UUID, detection: Detection) -> None:
        pass


class Harness:
    def __init__(self, **options: Any) -> None:
        self.host = Host()
        config: dict[str, Any] = {
            "silence_timer_ms": 0,
            "speculative_start_ms": 0,
            "final_wait_max_ms": 1000,
            "early_interval_ms": 10000,
            "early_all_modes": True,
            "early_route_selection": True,
            "keep_added_words": 2,
            "strict_extensions": True,
        }
        config.update(options)
        self.turns = TurnDetector(self.host, **config)

    def result(self, text: str, *, final: bool = False, flushed: bool = False) -> None:
        self.turns.on_result(TimedResult(text, final, flushed, 0, 100, "fixture"))

    def start(self, text: str = QUESTION) -> None:
        self.turns.on_speech_start()
        self.result(text)

    def final(self, text: str = QUESTION) -> None:
        self.result(text, final=True, flushed=True)

    async def request(self) -> tuple[str, asyncio.Future[Detection]]:
        return await asyncio.wait_for(self.host.requests.get(), 1)

    async def classified(self) -> tuple[UUID, str, Detection]:
        return await asyncio.wait_for(self.host.classifications.get(), 1)

    async def close(self) -> None:
        tasks = [task for task in (self.turns.task, *self.turns._detached) if task is not None]
        self.turns.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        detections = [future for _, future in self.host.calls]
        for future in detections:
            future.cancel()
        for match in self.host.matches:
            match.cancel()
        await asyncio.gather(*detections, return_exceptions=True)
        await asyncio.gather(*self.host.matches, return_exceptions=True)


async def settle() -> None:
    # Let synchronous Future callbacks and the cancellation they initiate run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.parametrize("already_done", [False, True])
async def test_classification_precedes_speech_end_and_adoption_keeps_display_gate(already_done):
    env = Harness()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_release(turn):
        entered.set()
        await release.wait()

    env.turns._wait_release = wait_release
    if already_done:
        env.host.immediate = EXPERT
    try:
        env.start()
        _, decision = await env.request()
        if not decision.done():
            decision.set_result(EXPERT)
        qid, text, _ = await env.classified()
        assert env.turns.speaking
        assert text == QUESTION
        assert env.host.shown == []
        assert env.host.confirmed.empty()
        env.turns.on_speech_end()
        await asyncio.wait_for(entered.wait(), 1)
        assert env.turns.turn is not None
        assert env.turns.turn.cand is not None
        assert env.turns.turn.cand.route_selected
        assert env.host.retimed == [qid]
        assert len(env.host.classified) == 1
        env.final()
        assert env.host.shown == []
        release.set()
        assert await asyncio.wait_for(env.host.confirmed.get(), 1) == (qid, QUESTION)
        assert env.host.shown == [(qid, QUESTION)]
    finally:
        release.set()
        await env.close()


async def test_completion_during_speech_end_transfer_uses_owned_turn_fallback_once():
    env = Harness()
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        # The done callback is queued, but this synchronous transfer retires its owner.
        env.turns.on_speech_end()
        env.final()
        qid, _, _ = await env.classified()
        assert await asyncio.wait_for(env.host.confirmed.get(), 1) == (qid, QUESTION)
        assert len(env.host.classified) == 1
    finally:
        await env.close()


@pytest.mark.parametrize("outcome", ["none", "wait", "duplicate", "error", "cancel", "exception"])
async def test_unqualified_detector_completion_never_selects_hidden_route(outcome):
    env = Harness()
    try:
        env.start()
        _, decision = await env.request()
        if outcome == "cancel":
            decision.cancel()
        elif outcome == "exception":
            decision.set_exception(RuntimeError("fixture"))
        elif outcome == "error":
            decision.set_result(Detection("answer", "technical", error="fixture"))
        else:
            decision.set_result(Detection(outcome, "other"))
        await settle()
        assert env.host.classified == []
        assert env.host.shown == []
    finally:
        await env.close()


@pytest.mark.parametrize("action", ["continuation", "cancel", "reset", "language", "new_owner"])
async def test_queued_completion_cannot_classify_a_retired_or_changed_early_owner(action):
    env = Harness()
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        if action == "continuation":
            env.result(QUESTION + " without a durable key")
        elif action == "language":
            await env.turns.change_language("es")
        elif action == "new_owner":
            env.turns.early_at = 0
            env.result(QUESTION + " Explain cancellation too.")
        else:
            getattr(env.turns, action)()
        await settle()
        assert env.host.classified == []
        assert env.host.shown == []
    finally:
        await env.close()


async def test_disabled_flag_never_calls_the_new_host_api():
    env = Harness(early_route_selection=False)

    def forbidden(qid: UUID, text: str, detection: Detection) -> bool:
        raise AssertionError("The optional host API must not be used when disabled")

    env.host.classify_held = forbidden
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        await settle()
        env.turns.on_speech_end()
        env.final()
        await asyncio.wait_for(env.host.confirmed.get(), 1)
        assert env.host.classified == []
    finally:
        await env.close()


@pytest.mark.parametrize("replace_route", [False, True])
async def test_only_actual_route_replacement_invalidates_punctuation_extension_reuse(replace_route):
    env = Harness()
    env.host.replace_route = replace_route
    revised = QUESTION.rstrip(".") + "?"
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        old, _, _ = await env.classified()
        env.final(revised)
        env.turns.on_speech_end()
        text, final_decision = await env.request()
        assert text == revised
        final_decision.set_result(Detection("answer", "other", depth="light"))
        env.result("", flushed=True)
        qid, text = await asyncio.wait_for(env.host.confirmed.get(), 1)
        assert text == revised
        assert (qid != old) is replace_route
        assert (old in env.host.discarded) is replace_route
        assert env.host.matches[0].cancelled() is replace_route
    finally:
        await env.close()


@pytest.mark.parametrize(
    "revised",
    [
        QUESTION.rstrip(".") + suffix
        for suffix in ("?", ", please?", " without a durable key?", " not after commit?", "!")
    ]
    + [QUESTION.replace("worker", "Worker")],
)
async def test_revision_during_display_wait_rebuilds_classified_generation_and_match(revised):
    env = Harness()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_release(turn):
        entered.set()
        await release.wait()

    env.turns._wait_release = wait_release
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        old, _, _ = await env.classified()
        env.turns.on_speech_end()
        await asyncio.wait_for(entered.wait(), 1)
        env.final(revised)
        release.set()
        text, final_decision = await env.request()
        assert text == revised
        assert old in env.host.discarded
        assert env.host.matches[0].cancelled()
        assert not env.host.matches[1].cancelled()
        final_decision.set_result(Detection("answer", "other", depth="light"))
        qid, text = await asyncio.wait_for(env.host.confirmed.get(), 1)
        assert qid != old
        assert text == revised
        assert all(shown[0] != old for shown in env.host.shown)
    finally:
        release.set()
        await env.close()


@pytest.mark.parametrize("replace_route", [False, True])
@pytest.mark.parametrize(
    "revised", [QUESTION.replace("worker", "Worker"), QUESTION.rstrip(".") + "!"]
)
async def test_exact_final_text_rebuilds_only_selected_route_before_adoption(
    replace_route, revised
):
    env = Harness()
    env.host.replace_route = replace_route
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        old, _, _ = await env.classified()
        env.final(revised)
        env.turns.on_speech_end()
        if replace_route:
            text, final_decision = await env.request()
            assert text == revised
            final_decision.set_result(EXPERT)
        env.result("", flushed=True)
        qid, text = await asyncio.wait_for(env.host.confirmed.get(), 1)
        assert text == revised
        assert (qid != old) is replace_route
        assert (old in env.host.discarded) is replace_route
        assert env.host.matches[0].cancelled() is replace_route
        assert len(env.host.calls) == (2 if replace_route else 1)
    finally:
        await env.close()


@pytest.mark.parametrize("final_answer", [False, True])
@pytest.mark.parametrize(
    "revised",
    [QUESTION.rstrip(".") + "?", QUESTION.replace("worker", "Worker"), QUESTION.rstrip(".") + "!"],
)
async def test_revision_after_release_withdraws_selected_route_before_confirmation(
    final_answer, revised
):
    env = Harness()
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        old, _, _ = await env.classified()
        env.turns.on_speech_end()
        assert await asyncio.wait_for(env.host.publications.get(), 1) == (old, QUESTION)
        assert env.host.confirmed.empty()
        env.final(revised)
        text, final_decision = await env.request()
        assert text == revised
        assert old in env.host.cancelled
        final_decision.set_result(Detection("answer" if final_answer else "none", "other"))
        assert env.turns.task is not None
        await asyncio.wait_for(env.turns.task, 1)
        if final_answer:
            qid, text = env.host.confirmed.get_nowait()
            assert qid != old
            assert text == revised
        else:
            assert env.host.confirmed.empty()
            assert env.host.shown == [(old, QUESTION)]
    finally:
        await env.close()


async def test_cancellation_owns_the_replacement_detector_and_match():
    env = Harness()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_release(turn):
        entered.set()
        await release.wait()

    env.turns._wait_release = wait_release
    try:
        env.start()
        _, decision = await env.request()
        decision.set_result(EXPERT)
        await env.classified()
        env.turns.on_speech_end()
        await asyncio.wait_for(entered.wait(), 1)
        env.final(QUESTION.rstrip(".") + "?")
        release.set()
        _, replacement = await env.request()
        task = env.turns.task
        env.turns.cancel()
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        assert replacement.cancelled()
        assert all(match.cancelled() for match in env.host.matches)
        assert env.host.held[-1][0] in env.host.discarded
        assert env.host.shown == []
    finally:
        release.set()
        await env.close()


async def test_forced_takeover_prevents_automatic_hidden_classification():
    env = Harness()
    try:
        env.start(QUESTION.rstrip("."))
        assert env.host.calls == []  # Unpunctuated stable drafts remain disabled.
        env.turns.on_speech_end()
        _, decision = await env.request()
        taken = env.turns.take_over()
        assert taken is not None
        assert taken[2] is decision
        decision.set_result(EXPERT)
        await settle()
        assert env.host.classified == []
        assert env.host.shown == []
        assert not env.host.matches[0].cancelled()
    finally:
        await env.close()


async def test_detached_completed_turn_uses_its_own_text_for_hidden_selection():
    env = Harness()
    text = QUESTION.rstrip(".")
    try:
        env.start(text)
        env.turns.on_speech_end()
        _, decision = await env.request()
        env.final(text)
        env.turns.on_speech_start()
        env.result("An unrelated speaker has started another explanation")
        decision.set_result(EXPERT)
        qid, selected_text, _ = await env.classified()
        assert selected_text == text
        assert env.turns.detached_turns == 1
        assert await asyncio.wait_for(env.host.confirmed.get(), 1) == (qid, text)
    finally:
        await env.close()


@pytest.mark.parametrize("outcome", ["cancel", "exception"])
async def test_retired_callback_drains_failed_future_without_side_effects(outcome):
    env = Harness()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    try:
        env.start()
        _, decision = await env.request()
        if outcome == "cancel":
            decision.cancel()
        else:
            decision.set_exception(RuntimeError("retired detector fixture"))
        env.turns.cancel()
        await settle()
        env.host.calls.clear()
        del decision
        gc.collect()
        await settle()
        assert unhandled == []
        assert env.host.classified == []
        assert env.host.shown == []
    finally:
        loop.set_exception_handler(previous_handler)
        await env.close()
