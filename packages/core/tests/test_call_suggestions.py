from dataclasses import replace

import pytest

from copilot_core.call_suggestions import CallObservation, CallPhase, CallSuggestions

CALL = CallObservation("zoom", "process-1", "call-1", CallPhase.ACTIVE, True)


def test_debounce_dismiss_and_reconnect_deduplicate() -> None:
    engine = CallSuggestions()
    assert engine.observe(CALL, now=0) is None
    assert engine.observe(CALL, now=2) is None
    suggestion = engine.observe(CALL, now=3)
    assert suggestion is not None
    assert suggestion.title == "Call detected"
    engine.dismiss(CALL.key)
    assert not engine.accept(CALL.key)
    engine.observe(replace(CALL, phase=CallPhase.ENDED), now=4)
    assert engine.observe(CALL, now=5) is None
    assert engine.observe(CALL, now=100) is None
    assert engine.observe(replace(CALL, call_id="new-call"), now=101) is None
    assert engine.observe(replace(CALL, call_id="new-call"), now=104) is not None


@pytest.mark.parametrize(
    "observation",
    [
        replace(CALL, reliable_call_evidence=False),
        replace(CALL, own_audio=True),
        replace(CALL, phase=CallPhase.UNKNOWN),
    ],
)
def test_audio_activity_and_unreliable_signals_cannot_suggest(observation: CallObservation) -> None:
    engine = CallSuggestions(debounce=0)
    assert engine.observe(observation, now=0) is None
    assert not engine.accept(observation.key)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("enabled", False),
        ("notifications_enabled", False),
        ("do_not_disturb", True),
        ("active_session", True),
        ("paused_until", 100),
        ("disabled_applications", {"zoom"}),
    ],
)
def test_suppression_preferences(setting: str, value: object) -> None:
    engine = CallSuggestions(debounce=0)
    setattr(engine, setting, value)
    assert engine.observe(CALL, now=0) is None


def test_resume_and_multiple_instances() -> None:
    engine = CallSuggestions(debounce=0)
    engine.paused_until = 5
    assert engine.observe(CALL, now=0) is None
    assert engine.observe(CALL, now=5) is not None
    assert engine.observe(replace(CALL, instance_id="process-2"), now=5) is not None


def test_accept_is_one_shot_and_end_requires_explicit_evidence_and_grace() -> None:
    engine = CallSuggestions(debounce=0)
    assert engine.observe(CALL, now=0)
    assert engine.accept(CALL.key)
    assert not engine.accept(CALL.key)
    engine.active_session = True
    assert engine.end_offers(now=1000) == []  # silence does not end calls
    engine.observe(replace(CALL, phase=CallPhase.ENDED), now=1001)
    assert engine.end_offers(now=1010) == []
    assert engine.observe(CALL, now=1011) is None  # reconnect cancels end offer
    assert engine.end_offers(now=1030) == []
    engine.observe(replace(CALL, phase=CallPhase.ENDED), now=1031)
    assert engine.end_offers(now=1051) == [CALL.key]
    assert engine.active_session
    assert engine.end_offers(now=1052) == []


def test_time_validation() -> None:
    engine = CallSuggestions()
    engine.observe(CALL, now=10)
    with pytest.raises(ValueError, match="monotonic"):
        engine.observe(CALL, now=9)
    with pytest.raises(ValueError, match="finite"):
        CallSuggestions(debounce=float("nan"))
