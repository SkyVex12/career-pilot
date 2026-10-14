"""Turn detection (ARCHITECTURE.md, "Turn detection, step by step").

On a speech-end frame on the "them" channel, at t0:
1. the detector (Haiku) runs on the utterance so far, and prepared-answer matching starts. If
   the text looks like a question (`likely_question`: a question word, a "?" or a request such
   as "tell me", and at least `speculative_min_words` words), the fast pass starts at once too,
   held: its output stays on the gateway and nothing is sent;
2. once the detector finds a question for the user (and not before t0 +
   speculative_start_ms), it is sent with speculative: true and the held pass is released:
   what it buffered goes out at once and the rest streams. When the detector finds no
   question, the held pass is cancelled and the client never hears of it, so a false start
   never shows. Without a held pass the fast pass starts here;
3. speech starting again before confirmation cancels that work (question.cancelled, or nothing
   for a pass still held) and the text keeps accumulating;
4. at t0 + silence_timer_ms (or earlier, if the turn model says the turn is complete), once
   the STT final has arrived, the same question_id is confirmed with speculative: false;
5. not a question for the user: nothing is shown and the text stays in the transcript.
   If the interim did not look like a question but the final text differs, the final is
   checked once more (an interim often lacks the last words), with a held pass of its own,
   and a question found then is sent confirmed straight away.

The turn-detection model sits behind `TurnModel`; Stage 0 ships `NoTurnModel`, so only the
silence timer decides.
"""

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from copilot_gateway.stt import TimedResult


@dataclass(frozen=True)
class Detection:
    is_question: bool
    complete: bool
    addressed_to_user: bool
    qtype: str
    confidence: float
    latency_ms: int = 0
    error: str | None = None

    def fires(self, min_confidence: float) -> bool:
        return (
            self.is_question
            and self.complete
            and self.addressed_to_user
            and self.confidence >= min_confidence
            and self.qtype != "small_talk"
        )


QUESTION_WORDS = frozenset(
    {"what", "whats", "how", "hows", "why", "when", "where", "which", "who", "whose", "whom"}
)
REQUEST_CUES = (
    "can you", "could you", "would you", "will you", "do you", "did you", "have you",
    "are you", "were you", "is there", "is it", "should you", "tell me", "tell us",
    "walk me", "walk us", "describe", "explain", "talk me through", "give me", "give us",
    "curious", "wondering",
)  # fmt: skip


def likely_question(text: str, min_words: int = 3) -> bool:
    """A cheap local check on the text, before any model call: is a held fast pass worth it?

    Too short (greetings, acknowledgements) is no; a "?" from the STT's punctuation, a question
    word anywhere, or a request cue is yes. It only gates the speculative start: a question it
    lets through by mistake costs one discarded fast pass, and one it misses still gets its
    fast pass once the detector fires, only later.
    """
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    if len(words) < min_words:
        return False
    if "?" in text or QUESTION_WORDS.intersection(words):
        return True
    joined = " ".join(words)
    return any(cue in joined for cue in REQUEST_CUES)


class TurnModel(Protocol):
    async def turn_complete(self, utterance: str) -> bool | None:
        """True when the speaker is done, False when not, None when it cannot tell."""
        ...


class NoTurnModel:
    """Placeholder until a turn-detection model is chosen (open question in the doc)."""

    async def turn_complete(self, utterance: str) -> bool | None:
        return None


class TurnHost(Protocol):
    """What the turn detector needs from its session."""

    def detect(self, utterance: str) -> Awaitable[Detection]: ...

    def start_match(self, utterance: str) -> "asyncio.Task[object] | None": ...

    def begin_question(
        self,
        text: str,
        detection: Detection,
        *,
        speculative: bool,
        t0: float,
        match: "asyncio.Task[object] | None",
    ) -> UUID: ...

    def confirm_question(
        self, qid: UUID, text: str, turn_end_ms: int, final_ms: int | None
    ) -> None: ...

    def cancel_question(self, qid: UUID) -> None: ...

    def start_held(self, text: str, t0: float, match: "asyncio.Task[object] | None") -> UUID:
        """Start a fast pass whose output stays on the gateway until it is released."""
        ...

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        """The detector found a question: send it, then the held pass's output."""
        ...

    def discard_question(self, qid: UUID) -> None:
        """Cancel a held pass that was never shown; nothing is sent."""
        ...


class TurnDetector:
    def __init__(
        self,
        host: TurnHost,
        *,
        silence_timer_ms: int,
        speculative_start_ms: int,
        final_wait_max_ms: int,
        min_confidence: float,
        turn_model: TurnModel | None = None,
        hold: bool = True,
        hold_min_words: int = 3,
    ) -> None:
        self.host = host
        self.silence_s = silence_timer_ms / 1000
        self.speculative_s = speculative_start_ms / 1000
        self.final_wait_s = final_wait_max_ms / 1000
        self.min_confidence = min_confidence
        self.turn_model = turn_model or NoTurnModel()
        self.hold = hold
        self.hold_min_words = hold_min_words
        self.finals: list[str] = []
        self.interim = ""
        self.flushed = asyncio.Event()
        self.flushed_at: float | None = None  # when the STT final after the speech end arrived
        self.task: asyncio.Task[None] | None = None
        self.speculative_qid: UUID | None = None
        self.held_qid: UUID | None = None  # a held fast pass not released yet
        # Counters for metrics and tests.
        self.speculative_starts = 0
        self.speculative_cancels = 0
        self.final_rechecks = 0
        self.held_starts = 0
        self.held_released = 0
        self.held_discarded = 0

    def utterance(self) -> str:
        return " ".join([*self.finals, self.interim]).strip()

    def reset(self) -> None:
        self.finals.clear()
        self.interim = ""

    def on_result(self, r: TimedResult) -> None:
        if r.is_final:
            if r.text:
                self.finals.append(r.text)
            self.interim = ""
        else:
            self.interim = r.text
        if r.flushed:
            self.flushed_at = time.monotonic()
            self.flushed.set()

    def on_speech_start(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()  # step 3; the task sends question.cancelled if needed
        self.task = None

    def on_speech_end(self) -> None:
        self.on_speech_start()
        self.flushed.clear()
        self.flushed_at = None
        self.task = asyncio.create_task(self._turn(time.monotonic()))

    def cancel(self) -> None:
        self.on_speech_start()

    async def _sleep_until(self, deadline: float) -> None:
        delay = deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _wait_final(self, deadline: float) -> None:
        if self.flushed.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flushed.wait(), max(0.0, deadline - time.monotonic()))

    async def _turn_end(self, t0: float) -> None:
        """Step 4's condition: the silence timer (or the turn model), then the STT final."""
        timer = asyncio.create_task(self._sleep_until(t0 + self.silence_s))
        model = asyncio.create_task(self.turn_model.turn_complete(self.utterance()))
        try:
            done, _ = await asyncio.wait({timer, model}, return_when=asyncio.FIRST_COMPLETED)
            if model in done and not timer.done() and model.result() is not True:
                await timer
        finally:
            timer.cancel()
            model.cancel()
        await self._wait_final(t0 + self.silence_s + self.final_wait_s)

    def _final_ms(self, t0: float) -> int | None:
        """t0 to the STT final, for latency_ms; None if it has not arrived."""
        if self.flushed_at is None or not self.flushed.is_set():
            return None
        return max(0, int((self.flushed_at - t0) * 1000))

    def _hold(self, text: str, t0: float, match: "asyncio.Task[object] | None") -> None:
        """Step 1: a held fast pass alongside the detector, if the text looks like a question."""
        self._discard_held()
        if self.hold and likely_question(text, self.hold_min_words):
            self.held_starts += 1
            self.held_qid = self.host.start_held(text, t0, match)

    def _discard_held(self) -> None:
        if self.held_qid is not None:
            self.held_discarded += 1
            self.host.discard_question(self.held_qid)
            self.held_qid = None

    def _show(
        self,
        text: str,
        detection: Detection,
        *,
        speculative: bool,
        t0: float,
        match: "asyncio.Task[object] | None",
    ) -> UUID:
        """Send the question: release the held pass if there is one, else start the fast pass
        now (the local check held none, or holding is off)."""
        held, self.held_qid = self.held_qid, None
        if held is not None:
            self.held_released += 1
            self.host.release_question(held, text, detection, speculative=speculative)
            return held
        return self.host.begin_question(
            text, detection, speculative=speculative, t0=t0, match=match
        )

    async def _turn(self, t0: float) -> None:
        text = self.utterance()
        if not text:  # the interim has not arrived yet; wait for the flushed final
            await self._wait_final(t0 + self.final_wait_s)
            text = self.utterance()
            if not text:
                return
        detection_task = asyncio.ensure_future(self.host.detect(text))
        match = self.host.start_match(text)
        try:
            self._hold(text, t0, match)
            await self._sleep_until(t0 + self.speculative_s)
            detection = await detection_task
            if not detection.fires(self.min_confidence):
                self._discard_held()
                await self._turn_end(t0)
                final_text = self.utterance()
                if final_text and final_text != text:
                    # The interim was cut short (often the last words and the "?"); check
                    # the final once more before deciding it was not a question.
                    if match is not None:
                        match.cancel()
                    match = self.host.start_match(final_text)
                    self._hold(final_text, t0, match)
                    detection = await self.host.detect(final_text)
                    if detection.fires(self.min_confidence):
                        self.final_rechecks += 1
                        turn_end_ms = int((time.monotonic() - t0) * 1000)
                        qid = self._show(
                            final_text, detection, speculative=False, t0=t0, match=match
                        )
                        self.host.confirm_question(qid, final_text, turn_end_ms, self._final_ms(t0))
                    else:
                        self._discard_held()
                self.reset()  # step 5: the text stays in the transcript tail only
                return
            qid: UUID | None = None
            silence_over = time.monotonic() >= t0 + self.silence_s
            if not (silence_over and self.flushed.is_set()):
                qid = self._show(text, detection, speculative=True, t0=t0, match=match)
                self.speculative_qid = qid
                self.speculative_starts += 1
            await self._turn_end(t0)
            final_text = self.utterance() or text
            turn_end_ms = int((time.monotonic() - t0) * 1000)
            if qid is None:
                qid = self._show(final_text, detection, speculative=False, t0=t0, match=match)
            self.speculative_qid = None
            self.host.confirm_question(qid, final_text, turn_end_ms, self._final_ms(t0))
            self.reset()
        except asyncio.CancelledError:
            detection_task.cancel()
            if match is not None:
                match.cancel()
            self._discard_held()
            if self.speculative_qid is not None:
                self.speculative_cancels += 1
                self.host.cancel_question(self.speculative_qid)
                self.speculative_qid = None
            raise
