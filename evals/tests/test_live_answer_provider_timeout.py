"""SDK timeout is explicit, source-bound, and independent of the trial deadline."""

import json
import socket
from argparse import Namespace
from pathlib import Path

import pytest
from copilot_evals import live_answer_replay as replay

from copilot_core.pricing import ModelPrice
from copilot_gateway.config import Settings
from copilot_gateway.testing import Reply, ScriptedProvider


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    assert Path(replay.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])

    # Windows asyncio creates a local socket pair for its own wakeup pipe.
    def guard(original):
        def local_only(sock, address):
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                return original(sock, address)
            pytest.fail("External provider/network calls forbidden")

        return local_only

    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))


@pytest.mark.parametrize("override", [None, 30.0])
async def test_provider_factory_and_record_identity_keep_trial_deadline(
    tmp_path, monkeypatch, override
):
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_providers=["openai"],
        openai_detector_model="authored-fast",
        openai_detector_reasoning="none",
        openai_fast_model="authored-fast",
        openai_fast_reasoning="none",
        openai_deep_model="authored-deep",
        openai_deep_reasoning="low",
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        model_prices={
            "authored-fast": ModelPrice(input=1, cached=0.1, output=2),
            "authored-deep": ModelPrice(input=2, cached=0.2, output=3),
        },
    )
    captured = []
    provider = ScriptedProvider("openai", lambda *args: Reply(["An authored complete answer."]))

    def factory(actual, timeout_s):
        captured.append((actual, timeout_s))
        return [provider]

    monkeypatch.setattr(replay, "make_providers", factory)
    args = Namespace(
        architectures=["B"],
        reps=1,
        seed=1,
        concurrency=1,
        timeout=90.0,
        provider_timeout=override,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=2,
    )
    cases = replay.load_cases()[:1]
    await replay.execute_live(args, cases, settings, None, None, tmp_path)
    expected = 90.0 if override is None else override
    assert captured == [(settings, expected)]
    assert len(provider.calls) == 1
    identity = json.loads((tmp_path / "identity.json").read_text())
    assert identity["timeout_s"] == 90.0
    assert identity["provider_timeout_s"] == expected
    execution = replay.schedule_run_identity(args, identity)
    assert execution["timeout_s"] == 90.0
    assert execution["provider_timeout_s"] == expected
    trials = json.loads((tmp_path / "trials.json").read_text())
    assert trials[0]["status"] == "completed"
    runs = replay.normalized_runs(trials, cases, identity, [])
    assert len(runs) == 1
    assert runs[0].identity["provider_timeout_s"] == expected
    assert runs[0].identity["timeout_s"] == 90.0
    if override is not None:
        # Changing only SDK timeout must fail before even constructing another provider.
        args.provider_timeout = 31.0
        with pytest.raises(ValueError, match="configuration, or source changed"):
            await replay.execute_live(args, cases, settings, None, None, tmp_path)
        assert len(captured) == 1


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "30"])
async def test_invalid_provider_timeout_rejected_before_preflight_or_provider(value, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Preflight ran before timeout validation")

    monkeypatch.setattr(replay, "preflight", forbidden)
    with pytest.raises(ValueError, match="Invalid provider timeout"):
        await replay.execute(Namespace(timeout=90.0, provider_timeout=value))


def test_schedule_rejects_provider_identity_drift():
    identity = {
        "concurrency": 2,
        "timeout_s": 90.0,
        "provider_timeout_s": 30.0,
        "seed": 1,
        "code": {"evals/src/copilot_evals/live_answer_replay.py": "a" * 64},
        "git_head": "b" * 40,
        "git_diff_sha256": "c" * 64,
        "provider_configuration_sha256": "d" * 64,
    }
    with pytest.raises(ValueError, match="provider timeout differs"):
        replay.schedule_run_identity(Namespace(provider_timeout=31), identity)
    with pytest.raises(ValueError, match="provider timeout differs"):
        replay.schedule_run_identity(Namespace(), identity)
    identity["provider_timeout_s"] = True
    with pytest.raises(ValueError, match="Invalid provider timeout"):
        replay.schedule_run_identity(Namespace(provider_timeout=30), identity)


@pytest.mark.parametrize(("argv", "expected"), [([], None), (["--provider-timeout", "30"], 30.0)])
def test_cli_provider_timeout_is_optional_and_does_not_change_trial(argv, expected, monkeypatch):
    captured = []

    async def fake_execute(args):
        captured.append(args)

    monkeypatch.setattr(replay, "execute", fake_execute)
    assert replay.main(["--out", str(Path("unused-authored")), "--timeout", "90", *argv]) == 0
    assert captured[0].provider_timeout == expected
    assert captured[0].timeout == 90.0
