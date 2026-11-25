import asyncio
import time
from types import SimpleNamespace

import pytest
from copilot_evals import live_answer_checkpoints as candidate


@pytest.mark.parametrize("interval", [0, -0.1, float("nan"), float("inf"), -float("inf")])
def test_rejects_unbounded_or_nonpositive_interval(interval):
    with pytest.raises(ValueError, match="finite and positive"):
        candidate.LoopLagMonitor(interval)


def test_declares_requested_and_effective_resolution(monkeypatch):
    monkeypatch.setattr(
        time, "get_clock_info", lambda name: SimpleNamespace(resolution=0.015625)
    )
    monitor = candidate.LoopLagMonitor(0.01)
    row = monitor.summary()
    assert row["requested_interval_ms"] == 10
    assert row["interval_ms"] == 10
    assert row["clock_resolution_ms"] == 15.625
    assert row["timer_floor_ms"] == 31.25
    assert row["samples"] == 0
    assert row["p50_ms_interval"] is None


@pytest.mark.parametrize(
    ("resolution", "requested", "actual_wait"), [(0.015625, 0.05, 0.015625), (0.001, 0.01, 0.009)]
)
async def test_early_wakeups_never_become_zero_time_samples(
    monkeypatch, resolution, requested, actual_wait
):
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(
        time, "get_clock_info", lambda name: SimpleNamespace(resolution=resolution)
    )
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monitor = candidate.LoopLagMonitor(requested)
    observe = monitor.observe

    class Finished(Exception):
        pass

    def observed(lag):
        observe(lag)
        if monitor.samples == 2:
            raise Finished

    monitor.observe = observed

    async def early_sleep(delay):
        sleeps.append(delay)
        assert len(sleeps) <= 10
        clock[0] += min(actual_wait, max(resolution, delay - resolution))

    monkeypatch.setattr(asyncio, "sleep", early_sleep)
    with pytest.raises(Finished):
        await monitor._run()
    assert monitor.samples == 2
    assert monitor.early_wakeups >= 2
    assert clock[0] >= 2 * monitor.interval_s
    assert all(delay >= 2 * resolution for delay in sleeps)
    assert monitor.total_ms >= 0


async def test_true_stall_is_retained_without_subtraction(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        time, "get_clock_info", lambda name: SimpleNamespace(resolution=1e-9)
    )
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monitor = candidate.LoopLagMonitor(0.01)

    class Finished(Exception):
        pass

    observe = monitor.observe

    def observed(lag):
        observe(lag)
        raise Finished

    monitor.observe = observed

    async def stalled_sleep(delay):
        clock[0] += delay + 0.4

    monkeypatch.setattr(asyncio, "sleep", stalled_sleep)
    with pytest.raises(Finished):
        await monitor._run()
    assert monitor.samples == 1
    assert monitor.maximum_ms == pytest.approx(400)
    assert monitor.early_wakeups == 0


async def test_stop_retains_last_overdue_tick_once(monkeypatch):
    clock = [2.4]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monitor = candidate.LoopLagMonitor(0.01)
    monitor.next_due = 2.0
    await monitor.stop()
    await monitor.stop()
    assert monitor.samples == 1
    assert monitor.maximum_ms == pytest.approx(400)
    assert monitor.next_due is None


async def test_stop_before_due_does_not_invent_sample(monkeypatch):
    monkeypatch.setattr(time, "perf_counter", lambda: 1.0)
    monitor = candidate.LoopLagMonitor(0.01)
    monitor.next_due = 2.0
    await monitor.stop()
    assert monitor.samples == 0


async def test_live_short_monitor_has_bounded_sampling_and_stops():
    monitor = candidate.LoopLagMonitor(0.01)
    started = time.perf_counter()
    await monitor.start()
    await asyncio.sleep(0.25)
    await monitor.stop()
    elapsed = time.perf_counter() - started
    assert 1 <= monitor.samples <= elapsed / monitor.interval_s + 1
    assert monitor.task is not None
    assert monitor.task.done()
    count = monitor.samples
    await asyncio.sleep(0.05)
    assert monitor.samples == count


async def test_requested_one_ms_deadline_retains_twenty_five_ms_final_stall():
    monitor = candidate.LoopLagMonitor(0.001)
    await monitor.start()
    time.sleep(0.025)  # noqa: ASYNC251 - Keep an authored loop stall through cancellation.
    await monitor.stop()
    assert monitor.samples > 0
    assert monitor.maximum_ms >= 15
    assert sum(monitor.histogram.values()) == monitor.samples


async def test_observation_cadence_has_explicit_actual_measurements(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monitor = candidate.LoopLagMonitor(0.01)
    monitor.observe(0)
    assert monitor.summary()["mean_observation_interval_ms"] is None
    clock[0] = 1.03
    monitor.observe(2)
    clock[0] = 1.08
    monitor.observe(4)
    row = monitor.summary()
    assert row["mean_observation_interval_ms"] == pytest.approx(40)
    assert row["min_observation_interval_ms"] == pytest.approx(30)
    assert row["max_observation_interval_ms"] == pytest.approx(50)
