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

One detector call per turn: the decision on the interim text is reused unless the STT final
changes it materially (`materially_changed`: other words, or a "?" the interim lacked; an
interim often lacks the last words and the question mark). Then the detector, the prepared
match and the held pass restart on the final the moment it arrives, and the call on the
interim is cancelled, so a turn never waits for two detector calls in a row. When no text has
arrived by the speech end, the turn starts on the first interim or final that does.

Early decision (2026-10-30, standups and meetings): while the other side is still speaking,
each time the live text ends a sentence (at most once per `early_interval_ms`), step 1 already
runs on it. At the speech end, if the text has not changed materially since, the turn takes
that detector call and held pass over instead of starting its own, so the detector's ~1 s is
spent while the speaker is still talking. In the 2026-10-30 standup that second was on the
path of every question.

A turn left unfinished (2026-11-11): when the detector says "wait" (the utterance stops
mid-sentence) and the same speaker goes on within `carry_gap_ms`, the next decision is made
on both parts, so the question on the card is the whole question. In the 2026-11-10
interview two cards carried only a question's last words ("...that you're experienced
with?"): the first half had been dropped when its turn ended undecided.

Shown before the detector (2026-11-11, one-on-one calls only): once the STT final is in, a
held pass on a plain question (`plain_question`: it ends in "?", opens with a question word
or "tell me", is long enough, and is not about pay, location or work status) is shown at
once, speculative, while the detector is still deciding. The detector then types it, or
says no and the card is withdrawn (`question.cancelled`). The detector's ~1 s was on the
path of every card: across 151 real questions the first word came at 1.9 s (p50), and the
user had started answering before it on 56% of interview questions.

Kept on a longer final (2026-11-16): the STT final usually ends with one to three words the
interim text lacked (56 of 141 finals in October's benchmark runs), and every such final
restarted the held fast pass, so its first token always counted from the final, 0.7 s after
the speech end. When the new text only extends the held pass's text by at most
`keep_added_words` words at its end, the pass is kept and only the detector decides again on
the whole text; the card shows the whole question. The same holds for a pass started early,
on a sentence end while the speaker was still talking, which may now happen in every call
(`early_all_modes`), not only in group calls.

The turn-detection model sits behind `TurnModel`; Stage 0 ships `NoTurnModel`, so only the
silence timer decides.
"""

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID

from copilot_core.languages import has_question_cue, speech_units
from copilot_gateway.stt import TimedResult


@dataclass(frozen=True)
class Detection:
    """The detector's decision: "answer" (a complete question for the user that needs an
    answer now), "wait" (it would be one, but stops mid-sentence) or "none"."""

    decision: str
    qtype: str
    latency_ms: int = 0
    error: str | None = None
    # The speech end to this decision, set by the turn that used it. Unlike `latency_ms` (the
    # call's own length) it includes the wait for the text and whatever ran before the speech
    # end, so it is the detector's real place on the path of the card.
    decided_ms: int | None = None
    # How much reasoning the detailed answer needs, "deep" or "light" (the Decisions API's
    # third question); None when the detector did not say, and the type decides.
    depth: str | None = None

    def fires(self) -> bool:
        return self.decision == "answer"


QUESTION_WORDS = frozenset(
    {"what", "whats", "how", "hows", "why", "when", "where", "which", "who", "whose", "whom"}
)
REQUEST_CUES = (
    "can you", "could you", "would you", "will you", "do you", "did you", "have you",
    "are you", "were you", "is there", "is it", "should you", "tell me", "tell us",
    "walk me", "walk us", "describe", "explain", "talk me through", "give me", "give us",
    "curious", "wondering",
)  # fmt: skip


# A turn left unfinished carries into the next at most this many times in a row, and only
# while it is short: a question in two or three parts, not a long explanation.
CARRY_MAX = 2
CARRY_MAX_WORDS = 60
# What a question shown before the detector decided is typed until it does.
FIRST = Detection("answer", "other")


def likely_question(text: str, min_words: int = 3, spoken_language: str = "en") -> bool:
    """A cheap local check on the text, before any model call: is a held fast pass worth it?

    Too short (greetings, acknowledgements) is no; a "?" from the STT's punctuation, a question
    word anywhere, or a request cue is yes. It only gates the speculative start: a question it
    lets through by mistake costs one discarded fast pass, and one it misses still gets its
    fast pass once the detector fires, only later.
    """
    if speech_units(text, spoken_language) < min_words:
        return False
    if spoken_language != "en":
        return any(mark in text for mark in "?\uff1f\u061f") or has_question_cue(
            text, spoken_language
        )
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    if "?" in text or QUESTION_WORDS.intersection(words):
        return True
    joined = " ".join(words)
    return any(cue in joined for cue in REQUEST_CUES)


# Words that may open a question before its question word ("okay, and then what about...").
LEAD_INS = frozenset(
    {"and", "so", "then", "okay", "ok", "alright", "right", "perfect", "great", "good", "cool",
     "awesome", "now", "well", "yeah", "yes", "sure", "got", "it", "gotcha", "also", "but",
     "thanks", "thank", "you", "um", "uh", "next", "nice"}
)  # fmt: skip
ASK_VERBS = frozenset(
    {"tell", "describe", "explain", "walk", "talk", "share", "give", "summarize", "outline"}
)
# A question on these is the user's to answer in a word, or the detector types it "none" (a
# screening check): never worth showing before the detector has decided.
LOGISTICS = frozenset(
    {"eligibility", "eligible", "authorized", "authorization", "sponsorship", "visa",
     "citizen", "relocate", "relocation", "salary", "compensation", "pay", "rate", "notice",
     "located", "location", "available", "availability", "start", "currently", "hear",
     "screen", "questions"}
)  # fmt: skip


def plain_question(text: str, min_words: int = 8) -> bool:
    """A question plain enough to show before the detector has decided (one-on-one calls):
    it ends in a "?" from the STT's punctuation, has at least `min_words` words, opens with a
    question word or a request to tell ("what", "how", "tell me", "can you describe") after
    at most a few lead-in words, and is not about pay, location or work status. Yes-or-no
    openers ("are you", "do you have") are left to the detector: screening checks start
    that way. A wrong guess shows a card that is then withdrawn."""
    if not text.rstrip().endswith("?"):
        return False
    words = _words(text)
    if len(words) < min_words or LOGISTICS.intersection(words):
        return False
    i = 0
    while i < len(words) and i < 6 and words[i] in LEAD_INS:
        i += 1
    head = words[i : i + 4]
    if not head:
        return False
    if head[0] in QUESTION_WORDS or head[0] in ASK_VERBS:
        return True
    if head[0] in ("can", "could", "would") and len(head) > 2 and head[1] == "you":
        return bool(ASK_VERBS.intersection(head[2:]))
    return False


def recoverable_question(text: str) -> str | None:
    """Conservative fallback for an unavailable or overly hesitant detector.

    Unlike the speculative-start heuristic, this requires a direct ask at a sentence
    boundary. A question word inside an explanation is not enough. An unfinished object
    ("could you explain the") still needs its continuation. This is only used one-on-one;
    the model remains responsible for addressees in group calls.
    """
    greeting = False
    for sentence in re.split(r"[.!?;]\s*", text.lower()):
        words = _words(sentence)
        if not words:
            continue
        if " ".join(words) in {"hi", "hello", "hey", "good morning", "good afternoon"}:
            greeting = True
            continue
        while words and words[0] in LEAD_INS:
            words = words[1:]
        if not words:
            continue
        head = " ".join(words)
        if head in {
            "how are you",
            "how are you doing",
            "how are you today",
            "how are you doing today",
            "how is it going",
            "how's it going",
            "how is your day",
            "how was your weekend",
        }:
            greeting = True
            continue
        if words[-1] in {
            "a",
            "an",
            "the",
            "your",
            "our",
            "my",
            "their",
            "and",
            "or",
            "because",
            "if",
            "whether",
            "we",
            "i",
            "you're",
            "we're",
            "they're",
            "i'm",
        }:
            continue
        if len(words) <= 4 and words[-1] in {"of", "with", "from", "to", "about", "for"}:
            continue
        if (
            len(words) >= 2
            and words[-1] == "you"
            and words[-2]
            in {
                "would",
                "could",
                "will",
                "can",
                "did",
                "do",
            }
        ):
            continue
        # Leading wh-clauses can be exposition: "what we do is ..." or "how we
        # deploy is documented ...". Their subject precedes the main copula; ordinary
        # direct questions invert that order ("what do we do", "how are we ...").
        # Only the outage/wait fallback uses this guard; a positive model decision wins.
        if (
            len(words) >= 4
            and words[0] in QUESTION_WORDS
            and words[1].split("'", 1)[0]
            in {"i", "we", "you", "they", "he", "she", "it", "my", "our", "your", "their"}
            and any(word in {"is", "are", "was", "were"} for word in words[2:])
        ):
            continue
        if head in {"why", "why not", "how so"}:
            return "other"
        if len(words) < 3:
            continue
        direct = words[0].replace("'", "") in QUESTION_WORDS or words[0] in ASK_VERBS
        direct = direct or any(
            head.startswith(cue + " ")
            for cue in REQUEST_CUES
            if cue not in {"curious", "wondering"}
        )
        if direct:
            return "other"
    return "small_talk" if greeting else None


# How the user opens their own update in a standup, in its first words.
UPDATE_OPENING = re.compile(
    r"\b(?:my|our) updates?\b|\b(?:from|on) my (?:side|end)\b|\bas for me\b"
    r"|\byesterday,? (?:i|we)\b|\bso,? yesterday\b|\btoday,? i(?:'m| am| will|'ll)\b",
    re.IGNORECASE,
)
UPDATE_OPENING_WORDS = 14


def update_opening(text: str) -> bool:
    """Is the user starting their own standup update ("so for my updates, yesterday I...")?
    Looked for in their first words only. In three of eight real standups the update began
    after the teammate simply stopped, with nothing on screen (2026-11-11 analysis)."""
    head = " ".join(text.split()[:UPDATE_OPENING_WORDS])
    return bool(UPDATE_OPENING.search(head))


# A statement that ends in a tag ("..., right?") is not a question of the user's.
TAG_QUESTION = re.compile(
    r"(?:^|[,\s])(?:right|okay|ok|yeah|correct|no|you know|isn't it|is it)\s*\?$", re.IGNORECASE
)


def asking(text: str) -> bool:
    """Is the user asking something rather than answering ("sorry, could you say that
    again?", "what is the planned date?")? One of their first three sentences is a question
    of three words or more; a "right?" tag is not one."""
    sentences = [s.strip() for s in re.findall(r"[^.?!]+[.?!]?", text)[:3]]
    return any(
        s.endswith("?") and not TAG_QUESTION.search(s) and len(_words(s)) >= 3 for s in sentences
    )


# A name right after one of these is a mention ("I'll check with Sam"), not an address.
MENTION_CUES = frozenset(
    {"with", "to", "from", "for", "ask", "tell", "about", "and", "or", "by", "of", "ping",
     "cc", "let", "thanks", "thank", "hi", "hello", "hey", "morning", "bye"}
)  # fmt: skip


def addressed_by_name(text: str, first: str) -> bool:
    """Does the utterance put a question or a check to the user by name ("..., right, Sam?",
    "Sam, did you get to the review?")? The name must stand as a vocative: followed by a
    comma or "?", or ending the utterance, and not after a word that makes it a mention or a
    greeting. And the utterance must look like a question (`likely_question`), so "Hi, Sam."
    is not one. A local check: it decides when the detector is slow or fails."""
    first = first.strip().lower()
    if not first or not likely_question(text, 2):
        return False
    tokens = re.findall(r"[\w']+|[,?!.]", text.lower())
    for i, tok in enumerate(tokens):
        if tok != first:
            continue
        before = next((t for t in reversed(tokens[:i]) if t not in ",.!?"), "")
        if before in MENTION_CUES:
            continue
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        last_word = not any(t not in ",.!?" for t in tokens[i + 1 :])
        if after in (",", "?") or last_word:
            return True
    return False


# Words that carry no answer: fillers, acknowledgements, pleasantries and the goodbye.
PLEASANTRIES = frozenset(
    {"yeah", "yes", "yep", "no", "ok", "okay", "so", "um", "uh", "mhmm", "hmm", "right",
     "sure", "well", "like", "you", "know", "i", "mean", "a", "the", "and", "to", "it", "is",
     "that", "for", "me", "too", "thanks", "thank", "great", "good", "nice", "day", "have",
     "bye", "sounds", "meet", "meeting", "talking", "speaking", "with", "was", "doing", "how",
     "about", "hi", "hello", "hey", "morning", "afternoon", "fine", "of", "course", "totally",
     "absolutely", "cool", "awesome", "perfect", "alright", "all", "works", "same", "as",
     "appreciate", "your", "time", "care", "take", "see", "soon", "again", "got",
     "makes", "sense", "exactly", "definitely", "in", "touch", "be", "will"}
)  # fmt: skip


def substantive_words(text: str) -> int:
    """How many words of the user's say something: fillers, acknowledgements and
    pleasantries ("yeah, for sure, thank you, have a great day") do not count. Gates
    following the user (`Session._maybe_follow`)."""
    return sum(1 for w in _words(text) if w.strip("'") not in PLEASANTRIES)


def is_backchannel(text: str) -> bool:
    """A listener's "mhmm", "okay", "yeah, right": up to three words that carry nothing. Not a
    turn of its own when following the user: on 2026-10-30 a teammate's "mhmm" between the
    user's lines made a second follow card on the same turn, twice."""
    words = _words(text)
    return 0 < len(words) <= 3 and substantive_words(text) == 0


# How the other side ends a call. "Nice to meet you" is left out: it opens calls as often.
CLOSING = re.compile(
    r"\b(?:good-?bye|bye(?:-bye| bye| now)?|take care|talk (?:to you )?soon"
    r"|have a (?:great|good|nice|wonderful|lovely) (?:day|afternoon|evening|night|weekend|one)"
    r"|thanks? (?:you )?(?:so much |again )?for your time|you'll be hearing from (?:us|me))\b",
    re.IGNORECASE,
)


def is_closing(text: str) -> bool:
    """Is this line of the other side's a goodbye? An interview that has gone quiet after one
    ends sooner (`idle_end_after_close_s`): on 2026-10-29 a session outlived its call by an
    hour and answered the next candidate's interview from the wrong profile."""
    return bool(CLOSING.search(text))


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def materially_changed(checked: str, final: str) -> bool:
    """Does the final text need its own detector call, or does the decision on `checked` hold?
    Case and punctuation do not matter, except a "?" that appears only in the final."""
    if _words(checked) != _words(final):
        return True
    return "?" in final and "?" not in checked


@dataclass
class _Candidate:
    """The text the detector is deciding on, with its call and its prepared-answer match."""

    text: str
    detection: "asyncio.Future[Detection]"
    match: "asyncio.Task[object] | None"

    def cancel(self) -> None:
        self.detection.cancel()
        if self.match is not None:
            self.match.cancel()


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

    def retime_held(self, qid: UUID, t0: float) -> None:
        """A held pass started before the speech end now belongs to the turn that ended at
        t0: its latencies count from there, as for any other turn."""
        ...

    def retype_question(self, qid: UUID, detection: Detection) -> None:
        """A question shown before the detector decided now has its type."""
        ...


@dataclass
class _Turn:
    """One speech end's work. Usually one at a time; a turn whose speaker had finished keeps
    deciding while the next utterance starts (see `TurnDetector.on_speech_start`)."""

    t0: float
    flushed: asyncio.Event = field(default_factory=asyncio.Event)
    flushed_at: float | None = None  # when the STT final after this speech end arrived
    text: str | None = None  # frozen when the turn is detached from the live text
    held_qid: UUID | None = None  # a held fast pass not released yet
    speculative_qid: UUID | None = None  # a question shown speculatively, not confirmed yet
    cand: _Candidate | None = None  # the text being decided, with its detector call
    taken: bool = False  # a forced question took the held pass and the detector call over
    early: "_Early | None" = None  # step 1 started on this text before the speech end
    first_qid: UUID | None = None  # shown before the detector decided (plain_question)


@dataclass
class _Early:
    """Step 1 started on the live text while the speaker was still talking."""

    cand: _Candidate
    held: UUID | None
    at: float


class TurnDetector:
    def __init__(
        self,
        host: TurnHost,
        *,
        silence_timer_ms: int,
        speculative_start_ms: int,
        final_wait_max_ms: int,
        turn_model: TurnModel | None = None,
        hold: bool = True,
        hold_min_words: int = 3,
        group_call: Callable[[], bool] | None = None,
        hold_any_min_words: int | None = None,
        early_interval_ms: int | None = None,
        carry_gap_ms: int = 0,
        first_show_min_words: int | None = None,
        keep_added_words: int | None = None,
        early_all_modes: bool = False,
        spoken_language: str = "en",
    ) -> None:
        self.host = host
        self.spoken_language = spoken_language
        self.silence_s = silence_timer_ms / 1000
        self.speculative_s = speculative_start_ms / 1000
        self.final_wait_s = final_wait_max_ms / 1000
        self.turn_model = turn_model or NoTurnModel()
        self.hold = hold
        self.hold_min_words = hold_min_words
        # In a group call (standup, meeting) a question to the user is often a statement ("so
        # you're good to start there"), which `likely_question` turns down: there, hold a pass
        # for any utterance of this many words (None: the local check only). On 2026-10-30, 3
        # of 8 questions waited ~0.9 s more for a fast pass started after the detector.
        self.group_call = group_call or (lambda: False)
        self.hold_any_min_words = hold_any_min_words
        # Early decision (module doc), group calls only; None: off.
        self.early_s = early_interval_ms / 1000 if early_interval_ms else None
        # A turn the detector left unfinished keeps its words for this long (module doc);
        # 0: off. At most CARRY_MAX turns in a row, so a long explanation is never glued.
        self.carry_s = carry_gap_ms / 1000
        self.carry_until: float | None = None
        self.carry_depth = 0
        # One-on-one calls: show a plain question before the detector decides; None: off.
        self.first_show_min_words = first_show_min_words
        # A held fast pass started on a text that the final (or the speech end's text) only
        # extends by at most this many words at its end is kept: only the detector and the
        # prepared match restart on the new text (module doc, "Kept on a longer final").
        # None: every change restarts the pass too.
        self.keep_added_words = keep_added_words
        # Early decision in every call, not only group calls.
        self.early_all_modes = early_all_modes
        self.speaking = False
        self.early: _Early | None = None
        self.early_at = 0.0
        self.finals: list[str] = []
        self.interim = ""
        self.text_arrived = asyncio.Event()  # any STT text since the last speech end
        self.turn: _Turn | None = None  # the turn the live text belongs to
        self.task: asyncio.Task[None] | None = None
        self._detached: set[asyncio.Task[None]] = set()
        # Counters for metrics and tests.
        self.speculative_starts = 0
        self.speculative_cancels = 0
        self.final_rechecks = 0
        self.held_starts = 0
        self.held_released = 0
        self.held_discarded = 0
        self.detached_turns = 0
        self.early_starts = 0
        self.early_used = 0
        self.carried = 0
        self.first_shown = 0
        self.first_withdrawn = 0
        self.held_kept = 0
        self.recovered = 0

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
        if r.text:
            self.text_arrived.set()
        if r.flushed and self.turn is not None:
            self.turn.flushed_at = time.monotonic()
            self.turn.flushed.set()
        if self.speaking and r.text:
            self._maybe_early()

    def _maybe_early(self) -> None:
        """Start step 1 on the live text when it ends a sentence (the module doc)."""
        if self.early_s is None or not (self.group_call() or self.early_all_modes):
            return
        text = self.utterance()
        if (
            not text
            or text[-1] not in ".?!\u3002\uff1f\uff01\u061f"
            or speech_units(text, self.spoken_language) < self.hold_min_words
        ):
            return
        if self.early is not None and not materially_changed(self.early.cand.text, text):
            return
        now = time.monotonic()
        if now - self.early_at < self.early_s:
            return
        self._drop_early()

        self.early_at = now
        self.early_starts += 1
        detection = asyncio.ensure_future(self.host.detect(text))
        duplicate = detection.done() and detection.result().decision == "duplicate"
        match = None if duplicate else self.host.start_match(text)
        held = None
        if not duplicate and self._should_hold(text):
            self.held_starts += 1
            held = self.host.start_held(text, now, match)
        self.early = _Early(_Candidate(text, detection, match), held, now)

    async def change_language(self, language: str) -> None:
        """Finish cancellation before a new recognition generation can deliver text."""
        tasks: list[Awaitable[object]] = [
            task for task in (self.task, *self._detached) if task is not None
        ]
        if self.early is not None:
            tasks.append(self.early.cand.detection)
            if self.early.cand.match is not None:
                tasks.append(self.early.cand.match)
        self.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.reset()
        self.turn = None
        self.speaking = False
        self.carry_until = None
        self.carry_depth = 0
        self.early_at = 0.0
        self.text_arrived.clear()
        self.spoken_language = language

    def _drop_early(self, early: "_Early | None" = None) -> None:
        if early is None:
            early, self.early = self.early, None
        if early is None:
            return
        early.cand.cancel()
        if early.held is not None:
            self.held_discarded += 1
            self.host.discard_question(early.held)

    def on_speech_start(self) -> None:
        self.speaking = True
        if self.carry_until is not None and time.monotonic() > self.carry_until:
            self.drop_carry()  # the speaker took too long: that sentence stays unfinished
        self._interrupt()

    def drop_carry(self) -> None:
        """Forget the words of a turn left unfinished: the pause was too long, or the user
        spoke in between."""
        if self.carry_until is None:
            return
        self.carry_until = None
        self.carry_depth = 0
        if self.task is None or self.task.done():
            self.reset()

    def _interrupt(self) -> None:
        task, turn = self.task, self.turn
        self.task = None
        if task is None or task.done():
            return
        if (
            turn is not None
            and turn.flushed.is_set()
            and time.monotonic() - turn.t0 >= self.silence_s
        ):
            # The silence outlasted the timer and the final is in: that speaker had finished,
            # and this is a new utterance, not more of the same one. The turn keeps deciding
            # on its own text (a slow detector must not glue utterances together), and the
            # new utterance starts from empty.
            turn.text = self.utterance()
            self.reset()
            self.detached_turns += 1
            self._detached.add(task)
            task.add_done_callback(self._detached.discard)
            return
        task.cancel()  # step 3; the task sends question.cancelled if needed

    def on_speech_end(self) -> None:
        self._interrupt()
        self.speaking = False
        self.text_arrived.clear()
        self.turn = _Turn(time.monotonic(), early=self.early)
        self.early = None
        self.task = asyncio.create_task(self._turn(self.turn))

    def cancel(self) -> None:
        for task in (self.task, *self._detached):
            if task is not None and not task.done():
                task.cancel()
        self.task = None
        self._drop_early()

    def take_over(self) -> "tuple[UUID, str, asyncio.Future[Detection]] | None":
        """A forced question with nothing typed, while the latest utterance's turn is still
        deciding and its held fast pass has not been shown: hand over that pass (with its
        prepared-answer match) and the detector call still running, instead of throwing both
        away. Returns the held question's id, its text and the detector call; None when there
        is nothing to take over. In the 2026-10-28 interview a Respond press 0.7 s after a
        question cancelled a detector that fires on it 5 of 5 times and a held pass with a
        prepared answer, and the new pass from scratch showed at 7.8 s."""
        turn, task = self.turn, self.task
        if (
            turn is None
            or task is None
            or task.done()
            or turn.held_qid is None
            or turn.speculative_qid is not None
            or turn.cand is None
        ):
            return None
        held, turn.held_qid = turn.held_qid, None
        turn.taken = True
        cand = turn.cand
        self.cancel()
        self.reset()
        return held, cand.text, cand.detection

    def _text(self, turn: _Turn) -> str:
        return turn.text if turn.text is not None else self.utterance()

    def _done(self, turn: _Turn) -> None:
        """The turn is decided; its text stays in the transcript tail only."""
        self.carry_until = None
        self.carry_depth = 0
        if turn.text is None:
            self.reset()

    def _carry(self, turn: _Turn) -> bool:
        """The detector said "wait": keep the turn's words for the same speaker's next
        ones. False when carrying is off or has gone on for CARRY_MAX turns already."""
        if self.carry_s <= 0 or self.carry_depth >= CARRY_MAX:
            return False
        words = self._text(turn).split()
        if not words or len(words) > CARRY_MAX_WORDS:
            return False
        self.carry_depth += 1
        self.carried += 1
        self.carry_until = time.monotonic() + self.carry_s
        if turn.text is not None:
            # A detached turn: the speaker's next words are already coming in on their own.
            self.finals.insert(0, turn.text)
        return True

    async def _sleep_until(self, deadline: float) -> None:
        delay = deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _wait_final(self, turn: _Turn, deadline: float) -> None:
        if turn.flushed.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(turn.flushed.wait(), max(0.0, deadline - time.monotonic()))

    async def _turn_end(self, turn: _Turn) -> None:
        """Step 4's condition: the silence timer (or the turn model), then the STT final."""
        timer = asyncio.create_task(self._sleep_until(turn.t0 + self.silence_s))
        model = asyncio.create_task(self.turn_model.turn_complete(self._text(turn)))
        try:
            done, _ = await asyncio.wait({timer, model}, return_when=asyncio.FIRST_COMPLETED)
            if model in done and not timer.done() and model.result() is not True:
                await timer
        finally:
            timer.cancel()
            model.cancel()
        await self._wait_final(turn, turn.t0 + self.silence_s + self.final_wait_s)

    async def _recover(self, turn: _Turn, detection: Detection) -> Detection:
        """A settled, identifiable ask must not vanish on wait or provider failure.

        Give a hesitant turn one second to resume. If it does, normal continuation owns
        its text; never recover that detached fragment as a second card. Explicit none
        and duplicate decisions remain authoritative.
        """
        if self.group_call() or (detection.decision != "wait" and not detection.error):
            return detection
        if turn.text is not None or recoverable_question(self._text(turn)) is None:
            return detection
        await self._turn_end(turn)
        if detection.decision == "wait":
            await self._sleep_until(turn.t0 + max(self.silence_s, 1.0))
        qtype = recoverable_question(self._text(turn))
        if turn.text is not None or qtype is None:
            return detection
        return replace(detection, decision="answer", qtype=qtype)

    @staticmethod
    def _final_ms(turn: _Turn) -> int | None:
        """t0 to the STT final, for latency_ms; None if it has not arrived."""
        if turn.flushed_at is None or not turn.flushed.is_set():
            return None
        return max(0, int((turn.flushed_at - turn.t0) * 1000))

    def _should_hold(self, text: str) -> bool:
        if not self.hold:
            return False
        if likely_question(text, self.hold_min_words, self.spoken_language):
            return True
        n = self.hold_any_min_words
        return n is not None and self.group_call() and speech_units(text, self.spoken_language) >= n

    def _hold(self, turn: _Turn, text: str, match: "asyncio.Task[object] | None") -> None:
        """Step 1: a held fast pass alongside the detector, if the text looks like a question
        (or, in a group call, is long enough to be one)."""
        self._discard_held(turn)
        if self._should_hold(text):
            self.held_starts += 1
            turn.held_qid = self.host.start_held(text, turn.t0, match)

    def _adopt(self, turn: _Turn, early: _Early) -> _Candidate:
        """Step 1 already ran on this text before the speech end: the turn takes it over."""
        self.early_used += 1
        turn.cand = early.cand
        turn.held_qid = early.held
        if early.held is not None:
            self.host.retime_held(early.held, turn.t0)
        return early.cand

    def _discard_held(self, turn: _Turn) -> None:
        if turn.held_qid is not None:
            self.held_discarded += 1
            self.host.discard_question(turn.held_qid)
            turn.held_qid = None

    def _show(
        self,
        turn: _Turn,
        text: str,
        detection: Detection,
        *,
        speculative: bool,
        match: "asyncio.Task[object] | None",
    ) -> UUID:
        """Send the question: release the held pass if there is one, else start the fast pass
        now (the local check held none, or holding is off)."""
        held, turn.held_qid = turn.held_qid, None
        if held is not None:
            self.held_released += 1
            self.host.release_question(held, text, detection, speculative=speculative)
            return held
        return self.host.begin_question(
            text, detection, speculative=speculative, t0=turn.t0, match=match
        )

    def _extends(self, checked: str, text: str) -> bool:
        """`text` is `checked` with at most `keep_added_words` more words at its end (or only
        a "?" more): the question the held pass answers is still the one being asked."""
        n = self.keep_added_words
        if n is None:
            return False
        before, after = _words(checked), _words(text)
        return bool(before) and after[: len(before)] == before and len(after) - len(before) <= n

    def _redetect(self, turn: _Turn, text: str, match: "asyncio.Task[object] | None") -> _Candidate:
        """The detector on the longer text; the held pass and its prepared match stay."""
        self.held_kept += 1
        detection = asyncio.ensure_future(self.host.detect(text))
        turn.cand = _Candidate(text, detection, match)
        return turn.cand

    def _start(self, turn: _Turn, text: str) -> _Candidate:
        """Step 1 on this text: the detector, the prepared match and a held fast pass."""
        detection = asyncio.ensure_future(self.host.detect(text))
        duplicate = detection.done() and detection.result().decision == "duplicate"
        match = None if duplicate else self.host.start_match(text)
        if not duplicate:
            self._hold(turn, text, match)
        turn.cand = _Candidate(text, detection, match)
        return turn.cand

    async def _wait_text(self, deadline: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.text_arrived.wait(), max(0.0, deadline - time.monotonic()))

    def _changed(self, turn: _Turn, cand: _Candidate) -> bool:
        """The STT final has arrived and its text needs a decision of its own."""
        if not turn.flushed.is_set():
            return False
        final = self._text(turn)
        return bool(final) and materially_changed(cand.text, final)

    async def _decide(self, turn: _Turn, cand: _Candidate) -> tuple[_Candidate, Detection]:
        """The detector's decision for the turn's newest text, with at most one call in flight:
        a final that changes the text replaces the candidate at once (cancelling the call on
        the interim); a final that does not keeps the interim's decision."""
        deadline = turn.t0 + self.silence_s + self.final_wait_s
        while True:
            if not cand.detection.done() and not turn.flushed.is_set():
                final = asyncio.ensure_future(turn.flushed.wait())
                try:
                    await asyncio.wait({cand.detection, final}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    final.cancel()
            elif not cand.detection.done():
                await asyncio.wait({cand.detection})
            if self._changed(turn, cand):
                self.final_rechecks += 1
                final = self._text(turn)
                if turn.held_qid is not None and self._extends(cand.text, final):
                    cand.detection.cancel()
                    cand = self._redetect(turn, final, cand.match)
                else:
                    cand.cancel()
                    cand = self._start(turn, final)
                continue
            if not cand.detection.done():
                continue  # the final came and changed nothing: the decision is still coming
            detection = cand.detection.result()
            if detection.fires() or turn.flushed.is_set():
                return cand, detection
            # No question in the interim, which may lack the last words: the final decides.
            await self._wait_final(turn, deadline)
            if not self._changed(turn, cand):
                return cand, detection

    async def _first(self, turn: _Turn) -> None:
        """Shown before the detector (module doc): once the STT final is in and the held pass
        is on that text, a plain question in a one-on-one call goes on screen while the
        detector is still deciding."""
        n = self.first_show_min_words
        if n is None or self.group_call():
            return
        await self._sleep_until(turn.t0 + self.speculative_s)
        await turn.flushed.wait()
        await asyncio.sleep(0.02)  # a final that changed the words restarts the candidate
        cand = turn.cand
        if cand is None or cand.detection.done() or turn.held_qid is None:
            return
        text = self._text(turn)
        if materially_changed(cand.text, text) or not plain_question(text, n):
            return
        self.first_shown += 1
        qid = self._show(turn, cand.text, FIRST, speculative=True, match=cand.match)
        turn.first_qid = turn.speculative_qid = qid
        self.host.retype_question(qid, FIRST)  # marked as shown first; typed for now

    async def _turn(self, turn: _Turn) -> None:
        t0 = turn.t0
        early, turn.early = turn.early, None
        early_longer = False
        if early is not None and (
            not self.utterance() or materially_changed(early.cand.text, self.utterance())
        ):
            if (
                self.utterance()
                and early.held is not None
                and self._extends(early.cand.text, self.utterance())
            ):
                early_longer = True  # a few more words: keep its pass, decide again
            else:
                self._drop_early(early)  # the speaker went on past that sentence
                early = None
        if not self.utterance():  # no text yet: start on the first interim or final
            await self._wait_text(t0 + self.final_wait_s)
            if not self.utterance():
                return
        cand: _Candidate | None = None
        try:
            if early is not None:
                cand = self._adopt(turn, early)
                if early_longer:
                    cand.detection.cancel()
                    cand = self._redetect(turn, self.utterance(), cand.match)
            else:
                cand = self._start(turn, self.utterance())
            first = asyncio.ensure_future(self._first(turn))
            try:
                cand, detection = await self._decide(turn, cand)
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            while True:
                recovered = await self._recover(turn, detection)
                # Recovery waits longer than the normal final deadline. A final arriving
                # in that interval must replace the stale held answer, not just its label.
                if self._changed(turn, cand):
                    cand, detection = await self._decide(turn, cand)
                    continue
                if recovered is not detection:
                    self.recovered += 1
                detection = recovered
                break
            detection = replace(detection, decided_ms=max(0, int((time.monotonic() - t0) * 1000)))
            if not detection.fires():
                self._discard_held(turn)
                if turn.first_qid is not None:  # shown before the detector said no
                    self.first_withdrawn += 1
                    self.host.cancel_question(turn.first_qid)
                    turn.first_qid = turn.speculative_qid = None
                if detection.decision != "wait" or not self._carry(turn):
                    self._done(turn)  # step 5
                return
            await self._sleep_until(t0 + self.speculative_s)
            qid: UUID | None = turn.first_qid
            silence_over = time.monotonic() >= t0 + self.silence_s
            if qid is not None:
                self.host.retype_question(qid, detection)
            elif not (silence_over and turn.flushed.is_set()):
                qid = self._show(turn, cand.text, detection, speculative=True, match=cand.match)
                turn.speculative_qid = qid
                self.speculative_starts += 1
            await self._turn_end(turn)
            final_text = self._text(turn) or cand.text
            turn_end_ms = int((time.monotonic() - t0) * 1000)
            if qid is None:
                qid = self._show(turn, final_text, detection, speculative=False, match=cand.match)
            turn.speculative_qid = None
            self.host.confirm_question(qid, final_text, turn_end_ms, self._final_ms(turn))
            self._done(turn)
        except asyncio.CancelledError:
            if cand is not None and not turn.taken:
                cand.cancel()
                await asyncio.gather(
                    cand.detection,
                    *([cand.match] if cand.match is not None else []),
                    return_exceptions=True,
                )
            self._discard_held(turn)
            if turn.speculative_qid is not None:
                self.speculative_cancels += 1
                self.host.cancel_question(turn.speculative_qid)
                turn.speculative_qid = turn.first_qid = None
            raise
