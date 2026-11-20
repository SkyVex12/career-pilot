"""Pure suggestion policy. Native adapters supply explicit call evidence; no audio capture."""

import math
from dataclasses import dataclass
from enum import StrEnum


class CallPhase(StrEnum):
    INCOMING = "incoming"
    ACTIVE = "active"
    ENDED = "ended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CallObservation:
    application_id: str
    instance_id: str
    call_id: str
    phase: CallPhase
    reliable_call_evidence: bool
    own_audio: bool = False
    reliable_title: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.application_id, self.instance_id, self.call_id


@dataclass(frozen=True)
class Suggestion:
    key: tuple[str, str, str]
    title: str
    message: str = "Start Copilot for live assistance."


@dataclass
class _Call:
    first_seen: float
    shown: bool = False
    dismissed: bool = False
    accepted: bool = False
    ended_at: float | None = None
    end_offered: bool = False


class CallSuggestions:
    """Use monotonic time and stable call IDs across devices/reconnects.

    Missing audio never ends a call. Adapters report ENDED explicitly. New calls
    require new IDs. This object emits intents; it never starts or ends sessions.
    """

    def __init__(
        self, *, debounce: float = 3, reconnect_grace: float = 20, retention: float = 3600
    ) -> None:
        if not all(math.isfinite(v) and v >= 0 for v in (debounce, reconnect_grace, retention)):
            raise ValueError("Durations must be finite and nonnegative")
        if retention < reconnect_grace:
            raise ValueError("Retention must cover reconnect grace")
        self.debounce = debounce
        self.reconnect_grace = reconnect_grace
        self.retention = retention
        self.enabled = True
        self.notifications_enabled = True
        self.do_not_disturb = False
        self.active_session = False
        self.paused_until = 0.0
        self.disabled_applications: set[str] = set()
        self._calls: dict[tuple[str, str, str], _Call] = {}
        self._now = -math.inf

    def _advance(self, now: float) -> None:
        if not math.isfinite(now) or now < self._now:
            raise ValueError("Use finite, monotonic timestamps")
        self._now = now
        self._calls = {
            key: call
            for key, call in self._calls.items()
            if call.ended_at is None or now - call.ended_at <= self.retention
        }

    def observe(self, observation: CallObservation, *, now: float) -> Suggestion | None:
        self._advance(now)
        if observation.own_audio or not observation.reliable_call_evidence:
            return None
        if not all(observation.key):
            raise ValueError("Application, instance and call identity are required")
        call = self._calls.get(observation.key)
        if observation.phase == CallPhase.ENDED:
            if call is not None and call.ended_at is None:
                call.ended_at = now
            return None
        if observation.phase not in (CallPhase.INCOMING, CallPhase.ACTIVE):
            return None
        if call is None:
            call = _Call(now)
            self._calls[observation.key] = call
        elif call.ended_at is not None:
            if now - call.ended_at > self.reconnect_grace:
                return None
            call.ended_at = None
        allowed = (
            self.enabled
            and self.notifications_enabled
            and not self.do_not_disturb
            and not self.active_session
            and now >= self.paused_until
            and observation.application_id not in self.disabled_applications
        )
        if (
            allowed
            and not (call.shown or call.dismissed)
            and now - call.first_seen >= self.debounce
        ):
            call.shown = True
            return Suggestion(observation.key, observation.reliable_title or "Call detected")
        return None

    def dismiss(self, key: tuple[str, str, str]) -> None:
        call = self._calls.get(key)
        if call is not None:
            call.dismissed = True

    def accept(self, key: tuple[str, str, str]) -> bool:
        """Return a one-shot intent for the existing user-driven session flow."""
        call = self._calls.get(key)
        if (
            call is None
            or not call.shown
            or call.dismissed
            or call.accepted
            or call.ended_at is not None
            or self.active_session
        ):
            return False
        call.accepted = True
        return True

    def end_offers(self, *, now: float) -> list[tuple[str, str, str]]:
        """Offer confirmation only; caller retains control of its live session."""
        self._advance(now)
        result: list[tuple[str, str, str]] = []
        for key, call in self._calls.items():
            if (
                self.active_session
                and call.accepted
                and not call.end_offered
                and call.ended_at is not None
                and now - call.ended_at >= self.reconnect_grace
            ):
                call.end_offered = True
                result.append(key)
        return result
