"""Bounded durable checkpoints for evaluation runners, with guarded provider admission."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot_evals.live_answer_artifacts import run_lock
from copilot_evals.live_answer_checkpoints import LoopLagMonitor, RecordStore
from copilot_evals.sensei_runtime import save

READ_KINDS = (
    "cost",
    "requests",
    "attempts",
    "faults",
    "trials",
    "sessions",
    "dispatch",
    "dispatch-plan",
)
ASYNC_POLICY = "atomic-records-async-request-fence-v2"


class CheckpointUnavailable(RuntimeError):
    """Local storage admission failure, never a provider/model failure."""


class CheckpointDrainTimeout(CheckpointUnavailable):
    """Writer still owns the output lock; caller must not export or declare completion."""


class StaleDurabilityBarrier(CheckpointUnavailable):
    pass


@dataclass(frozen=True)
class Receipt:
    sequence: int
    future: concurrent.futures.Future[Any]


@dataclass(frozen=True)
class DispatchPermit:
    sequence: int


REQUEST_FENCE_POLICY = "request-reservation-fence-v1"


@dataclass(frozen=True)
class RequestDispatchFence:
    owner: object
    request_key: str
    request_receipt: Receipt
    reservation_key: str
    reservation_receipt: Receipt
    target: Receipt


@dataclass(frozen=True)
class RequestDispatchPermit:
    fence: RequestDispatchFence


@dataclass(frozen=True)
class Command:
    receipt: Receipt
    kind: str
    key: str
    payload: bytes
    queued_at: float
    encode_ms: float
    operation: str = "record"
    phase: str = "active_workers"


class DurableWriter:
    """A single non-daemon worker owns RecordStore and its OS lock.

    Accepted snapshots never coalesce. Snapshot encoding is bounded CPU work on
    the event loop; serialization/flush/fsync/rename run on the owned thread.
    A cancelled waiter cannot cancel its write. No callback exception is needed
    for failure safety: the sticky flag is checked independently before dispatch.
    """

    def __init__(
        self,
        directory: Path,
        *,
        capacity: int = 256,
        byte_capacity: int = 16_000_000,
        on_failure: Callable[[str], None] | None = None,
        store_factory: Callable[[Path], RecordStore] = RecordStore,
        initialization_guard: Callable[[Path], None] | None = None,
    ):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Invalid record capacity")
        if type(byte_capacity) is not int or byte_capacity < 1:
            raise ValueError("Invalid byte capacity")
        self.directory = directory
        self.capacity, self.byte_capacity = capacity, byte_capacity
        self.loop = asyncio.get_running_loop()
        self.on_failure: Callable[[str], None] | None = on_failure
        self.store_factory = store_factory
        self.initialization_guard = initialization_guard
        self.anchor: dict[str, Any] = {
            "perf_counter_s": time.perf_counter(),
            "monotonic_s": time.monotonic(),
            "utc": datetime.now(UTC).isoformat(),
        }
        self._commands: queue.Queue[Command | tuple[tuple[str, str], ...]] = queue.Queue()
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._done: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        self._mutex = threading.Lock()
        self._failed = threading.Event()
        self._lock_owned = threading.Event()
        self._failure: BaseException | None = None
        self._reason: str | None = None
        self._closing = False
        self._last: Receipt | None = None
        self._dispatch_owner = object()
        self._record_receipts: dict[tuple[str, str], Receipt] = {}
        self._request_barriers: list[dict[str, Any]] = []
        self._omitted_request_barriers = 0
        self._committed = 0
        self._pending_count = self._pending_bytes = 0
        self.peak_count = self.peak_bytes = 0
        self.initial_records: dict[str, dict[str, Any]] = {}
        self.snapshot_errors: list[dict[str, Any]] = []
        self.omitted_snapshot_errors = 0
        self.backend: RecordStore | None = None
        self._samples: list[dict[str, Any]] = []
        self._barriers: list[dict[str, Any]] = []
        self._omitted_samples = self._omitted_barriers = 0
        self.thread = threading.Thread(
            target=self._worker, name="replay-durable-writer", daemon=False
        )
        self.thread.start()

    async def _await(self, future: concurrent.futures.Future[Any]) -> Any:
        wrapped = asyncio.wrap_future(future)
        # A cancelled waiter does not cancel ownership or create unobserved exceptions.
        wrapped.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        return await asyncio.shield(wrapped)

    async def ready(self) -> None:
        await self._await(self._ready)
        self.check_health()

    def _failure_notice(self) -> None:
        if self.on_failure is not None:
            # Observers may be swallowed; sticky admission remains authoritative.
            with suppress(BaseException):
                self.on_failure(self._reason or "checkpoint_error")

    def _fail(self, error: BaseException, reason: str) -> None:
        with self._mutex:
            first = not self._failed.is_set()
            if first:
                self._failure, self._reason = error, reason
                self._failed.set()
        if first:
            # A closing loop cannot reopen the sticky failure gate.
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._failure_notice)

    def check_health(self) -> None:
        if self._failed.is_set():
            raise CheckpointUnavailable(self._reason or "checkpoint_error") from self._failure

    def assert_initialized(self) -> None:
        self.check_health()
        if not self._ready.done():
            raise CheckpointUnavailable("checkpoint_writer_not_ready")
        self._ready.result()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    @property
    def failure_reason(self) -> str | None:
        return self._reason

    @property
    def lock_owned(self) -> bool:
        return self._lock_owned.is_set()

    def submit(
        self,
        kind: str,
        key: str,
        value: Any,
        *,
        operation: str = "record",
        phase: str = "active_workers",
    ) -> Receipt:
        if asyncio.get_running_loop() is not self.loop:
            raise ValueError("Writer submission must use its owning event loop")
        self.assert_initialized()
        if self._closing:
            raise CheckpointUnavailable("checkpoint_writer_closing")
        started = time.perf_counter()
        try:
            # Freeze before handing ownership to the worker. No live mutable alias escapes.
            payload = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode()
        except BaseException as error:
            self._fail(error, "checkpoint_snapshot_error")
            raise CheckpointUnavailable("checkpoint_snapshot_error") from error
        encoded = time.perf_counter()
        with self._mutex:
            full = (
                self._pending_count >= self.capacity
                or self._pending_bytes + len(payload) > self.byte_capacity
            )
            if not full:
                self._pending_count += 1
                self._pending_bytes += len(payload)
                self.peak_count = max(self.peak_count, self._pending_count)
                self.peak_bytes = max(self.peak_bytes, self._pending_bytes)
        if full:
            error = CheckpointUnavailable("checkpoint_queue_full")
            self._fail(error, "checkpoint_queue_full")
            raise error
        receipt = Receipt(
            (self._last.sequence if self._last else 0) + 1, concurrent.futures.Future()
        )
        self._last = receipt
        if operation == "record":
            self._record_receipts[(kind, key)] = receipt
        self._commands.put_nowait(
            Command(
                receipt, kind, key, payload, encoded, (encoded - started) * 1000, operation, phase
            )
        )
        return receipt

    def capture_request_dispatch(
        self, request_key: str, reservation_key: str
    ) -> RequestDispatchFence:
        """Capture both accepted versions synchronously after intent and reserve writes."""
        self.assert_initialized()
        if self._closing:
            raise CheckpointUnavailable("checkpoint_writer_closing")
        if (
            type(request_key) is not str
            or not request_key
            or type(reservation_key) is not str
            or not reservation_key
        ):
            raise StaleDurabilityBarrier("Request and reservation keys are required")
        request = self._record_receipts.get(("requests", request_key))
        reservation = self._record_receipts.get(("cost", reservation_key))
        if request is None or reservation is None:
            raise StaleDurabilityBarrier("Request and reservation must have accepted writes")
        target = max((request, reservation), key=lambda receipt: receipt.sequence)
        return RequestDispatchFence(
            self._dispatch_owner, request_key, request, reservation_key, reservation, target
        )

    def _validate_request_fence(self, fence: RequestDispatchFence) -> None:
        self.assert_initialized()
        if self._closing:
            raise CheckpointUnavailable("checkpoint_writer_closing")
        if type(fence) is not RequestDispatchFence or fence.owner is not self._dispatch_owner:
            raise StaleDurabilityBarrier("Request fence belongs to another writer")
        if (
            type(fence.request_key) is not str
            or type(fence.reservation_key) is not str
            or type(fence.request_receipt) is not Receipt
            or type(fence.reservation_receipt) is not Receipt
            or type(fence.target) is not Receipt
            or (
                self._record_receipts.get(("requests", fence.request_key))
                is not fence.request_receipt
            )
            or (
                self._record_receipts.get(("cost", fence.reservation_key))
                is not fence.reservation_receipt
            )
        ):
            raise StaleDurabilityBarrier("A bound request or reservation version changed")
        target = max(
            (fence.request_receipt, fence.reservation_receipt),
            key=lambda receipt: receipt.sequence,
        )
        if fence.target is not target:
            raise StaleDurabilityBarrier("Request fence has an invalid FIFO target")

    async def wait_request_dispatch(self, fence: RequestDispatchFence) -> RequestDispatchPermit:
        self._validate_request_fence(fence)
        started = time.perf_counter()
        outcome = "pending"
        try:
            await self._await(fence.target.future)
            permit = RequestDispatchPermit(fence)
            self.assert_request_dispatch_ready(permit)
            outcome = "ready"
            return permit
        except BaseException as error:
            outcome = type(error).__name__
            raise
        finally:
            sample = {
                "policy": REQUEST_FENCE_POLICY,
                "outcome": outcome,
                "target_committed": self._committed >= fence.target.sequence,
                "at_s": started - self.anchor["perf_counter_s"],
                "wait_ms": (time.perf_counter() - started) * 1000,
                "request_key_sha256": hashlib.sha256(fence.request_key.encode()).hexdigest(),
                "reservation_key_sha256": hashlib.sha256(
                    fence.reservation_key.encode()
                ).hexdigest(),
                "request_sequence": fence.request_receipt.sequence,
                "reservation_sequence": fence.reservation_receipt.sequence,
                "target_sequence": fence.target.sequence,
                "failure_reason": self._reason,
            }
            if len(self._request_barriers) < 50000:
                self._request_barriers.append(sample)
            else:
                self._omitted_request_barriers += 1

    def assert_request_dispatch_ready(self, permit: RequestDispatchPermit) -> None:
        if type(permit) is not RequestDispatchPermit:
            raise StaleDurabilityBarrier("A request dispatch permit is required")
        self._validate_request_fence(permit.fence)
        if self._committed < permit.fence.target.sequence:
            raise StaleDurabilityBarrier("Request fence is not durably committed")

    async def barrier(self) -> DispatchPermit:
        self.assert_initialized()
        started = time.perf_counter()
        try:
            while True:
                self.check_health()
                target = self._last
                if target is not None:
                    await self._await(target.future)
                self.check_health()
                # Writes added while waiting require a fresh barrier, never a stale receipt.
                if self._last is target:
                    return DispatchPermit(target.sequence if target else 0)
        finally:
            sample = {
                "at_s": started - self.anchor["perf_counter_s"],
                "wait_ms": (time.perf_counter() - started) * 1000,
                "failure_reason": self._reason,
            }
            if len(self._barriers) < 50000:
                self._barriers.append(sample)
            else:
                self._omitted_barriers += 1

    def assert_dispatch_ready(self, permit: DispatchPermit) -> None:
        self.assert_initialized()
        if self._closing:
            raise CheckpointUnavailable("checkpoint_writer_closing")
        sequence = self._last.sequence if self._last else 0
        if permit.sequence != sequence or self._committed < sequence:
            raise StaleDurabilityBarrier("New records require a fresh durability barrier")

    def _worker(self) -> None:
        store = None
        try:
            with run_lock(self.directory):
                self._lock_owned.set()
                if self.initialization_guard is not None:
                    self.initialization_guard(self.directory)
                store = self.store_factory(self.directory)
                self.backend = store
                for kind in READ_KINDS:
                    rows = store.read(kind)
                    self.initial_records[kind] = {
                        key: rows[ordinal] for key, ordinal in store.index[kind].items()
                    }
                self._ready.set_result(None)
                while True:
                    command = self._commands.get()
                    if isinstance(command, tuple):
                        self.check_health()
                        for kind, filename in command:
                            store.export(kind, filename)
                        break
                    started = time.perf_counter()
                    write_error = None
                    try:
                        self.check_health()
                        store.phase = command.phase
                        if command.operation == "record":
                            store.put(command.kind, command.key, json.loads(command.payload))
                        elif command.operation in ("snapshot", "progress"):
                            if Path(command.key).name != command.key or command.key in (".", ".."):
                                raise ValueError("Snapshot filename must remain inside the output")
                            try:
                                write_snapshot(
                                    self.directory / command.key,
                                    command.payload,
                                    durable=command.operation == "snapshot",
                                )
                            except OSError as error:
                                if command.operation != "progress":
                                    raise
                                # Only live progress is best effort; stop files are durable.
                                if len(self.snapshot_errors) >= 1000:
                                    self.omitted_snapshot_errors += 1
                                else:
                                    self.snapshot_errors.append(
                                        {
                                            "error_type": type(error).__name__,
                                            "winerror": getattr(error, "winerror", None),
                                            "filename": command.key,
                                            "at_s": started - self.anchor["perf_counter_s"],
                                        }
                                    )
                        else:
                            raise ValueError("Invalid private writer operation")
                        self._committed = command.receipt.sequence
                    except BaseException as error:
                        self._fail(error, "checkpoint_write_error")
                        write_error = CheckpointUnavailable(
                            self._reason or "checkpoint_write_error"
                        )
                        write_error.__cause__ = error
                    finally:
                        finished = time.perf_counter()
                        with self._mutex:
                            self._pending_count -= 1
                            self._pending_bytes -= len(command.payload)
                        sample = {
                            "sequence": command.receipt.sequence,
                            "kind": command.kind,
                            "key_sha256": hashlib.sha256(command.key.encode()).hexdigest(),
                            "queued_at_s": command.queued_at - self.anchor["perf_counter_s"],
                            "started_at_s": started - self.anchor["perf_counter_s"],
                            "encode_ms": command.encode_ms,
                            "queue_ms": (started - command.queued_at) * 1000,
                            "worker_ms": (finished - started) * 1000,
                            "failed": write_error is not None,
                        }
                        if len(self._samples) < 50000:
                            self._samples.append(sample)
                        else:
                            self._omitted_samples += 1
                    if write_error is None:
                        command.receipt.future.set_result(None)
                    else:
                        command.receipt.future.set_exception(write_error)
        except BaseException as error:
            self._fail(error, "checkpoint_worker_error")
            if not self._ready.done():
                self._ready.set_exception(error)
        finally:
            # This runs after run_lock exits. Never report final drain while it is owned.
            self._lock_owned.clear()
            self._done.set_result(
                {
                    "anchor": self.anchor,
                    "writes": self._samples,
                    "barriers": self._barriers,
                    "request_dispatch_policy": REQUEST_FENCE_POLICY,
                    "request_barriers": self._request_barriers,
                    "omitted_request_barriers": self._omitted_request_barriers,
                    "omitted_writes": self._omitted_samples,
                    "omitted_barriers": self._omitted_barriers,
                    "peak_pending_records": self.peak_count,
                    "peak_pending_bytes": self.peak_bytes,
                    "committed_sequence": self._committed,
                    "failure_reason": self._reason,
                    "original_error_type": type(self._failure).__name__ if self._failure else None,
                    "durable_store": store.telemetry() if store is not None else None,
                    "scope": (
                        "Observed snapshot CPU, FIFO queue, barrier and worker I/O; no subtraction"
                    ),
                }
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "policy": ASYNC_POLICY,
            "anchor": dict(self.anchor),
            "writes": list(self._samples),
            "barriers": list(self._barriers),
            "request_dispatch_policy": REQUEST_FENCE_POLICY,
            "request_barriers": list(self._request_barriers),
            "omitted_request_barriers": self._omitted_request_barriers,
            "omitted_writes": self._omitted_samples,
            "omitted_barriers": self._omitted_barriers,
            "peak_pending_records": self.peak_count,
            "peak_pending_bytes": self.peak_bytes,
            "pending_records": self._pending_count,
            "committed_sequence": self._committed,
            "failure_reason": self._reason,
            "lock_owned": self.lock_owned,
            "snapshot_errors": list(self.snapshot_errors),
            "omitted_snapshot_errors": self.omitted_snapshot_errors,
            "durable_store": self.backend.telemetry() if self.backend is not None else None,
            "scope": "Observed snapshot CPU, queue/barrier and I/O; no latency subtraction",
        }

    def assert_drained(self) -> None:
        self.check_health()
        if self._pending_count or self._committed != (self._last.sequence if self._last else 0):
            raise CheckpointUnavailable("Checkpoint export requires durable drain")

    async def close(
        self, *, timeout_s: float = 10, exports: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Any]:
        if not self._closing:
            self._closing = True
            self._commands.put_nowait(exports)
        try:
            result = await asyncio.wait_for(self._await(self._done), timeout_s)
        except TimeoutError as error:
            self._fail(error, "checkpoint_shutdown_timeout")
            raise CheckpointDrainTimeout("Writer remains alive and owns the output lock") from error
        await asyncio.to_thread(self.thread.join)
        # Startup failures retain their original diagnostic after ownership is released.
        # Active write failures still use the sticky storage-admission exception below.
        self._ready.result()
        self.check_health()
        return result


class AnchoredLoopLagMonitor(LoopLagMonitor):
    def __init__(self, interval_s: float = 0.01, spike_ms: float = 50, max_spikes: int = 1000):
        super().__init__(interval_s)
        self.anchor: dict[str, Any] = {
            "perf_counter_s": time.perf_counter(),
            "monotonic_s": time.monotonic(),
            "utc": datetime.now(UTC).isoformat(),
        }
        self.spike_ms, self.max_spikes = spike_ms, max_spikes
        self.spikes: list[dict[str, float]] = []
        self.omitted_spikes = 0

    def observe(self, lag: float) -> None:
        super().observe(lag)
        if lag >= self.spike_ms:
            if len(self.spikes) < self.max_spikes:
                self.spikes.append(
                    {
                        "observed_at_s": time.perf_counter() - self.anchor["perf_counter_s"],
                        "lag_ms": lag,
                    }
                )
            else:
                self.omitted_spikes += 1

    def summary(self) -> dict[str, Any]:
        return {
            **super().summary(),
            "anchor": self.anchor,
            "spike_threshold_ms": self.spike_ms,
            "spikes": self.spikes,
            "omitted_spikes": self.omitted_spikes,
        }


class BufferedRecordStore(RecordStore):
    """Existing synchronous record API, with all active writes owned by DurableWriter.

    Startup reads are captured under the worker's lock. Mutated cached values are
    exported only after a mandatory durable drain; this is not a drop-in backend
    for code that assumes put() itself has already reached disk.
    """

    def __init__(self, writer: DurableWriter):
        writer.assert_initialized()
        self.writer, self.directory = writer, writer.directory
        self.root = writer.directory / "checkpoint-records"
        self.phase = "startup"
        self.on_error = None
        self.cache = copy.deepcopy(writer.initial_records)

        def notify_failure(reason: str) -> None:
            if self.on_error is not None:
                self.on_error()

        writer.on_failure = notify_failure

    def put(self, kind: str, key: str, value: Any) -> None:
        self.writer.submit(kind, key, value, phase=self.phase)
        self.cache.setdefault(kind, {})[key] = copy.deepcopy(value)

    def read(self, kind: str) -> list[Any]:
        self.writer.check_health()
        if kind not in READ_KINDS:
            raise ValueError("Buffered startup read kind was not preloaded")
        return copy.deepcopy(list(self.cache.get(kind, {}).values()))

    async def flush(self) -> DispatchPermit:
        return await self.writer.barrier()

    def snapshot_file(self, filename: str, value: Any, *, best_effort: bool = False) -> None:
        if best_effort and filename != "progress.json":
            raise ValueError("Only progress.json may use best-effort snapshots")
        self.writer.submit(
            "snapshot",
            filename,
            value,
            operation="progress" if best_effort else "snapshot",
            phase=self.phase,
        )

    def export(self, kind: str, filename: str) -> None:
        self.writer.assert_drained()
        save(self.directory / filename, self.read(kind))

    def telemetry(self) -> dict[str, Any]:
        return self.writer.snapshot()


def require_async_directory(directory: Path) -> None:
    """Under the owned lock, reject old or unidentified artifacts before creating shards.

    Existing runs are immutable inputs to this policy change; resume requires an
    identical async identity plus its authoritative record manifest.
    """
    identity_path = directory / "identity.json"
    if identity_path.exists():
        identity = json.loads(identity_path.read_text("utf-8"))
        if identity.get("checkpoint_policy") != ASYNC_POLICY:
            raise ValueError("Existing run uses another checkpoint policy; use a fresh output")
        if not (directory / "checkpoint-records" / "manifest.json").exists():
            raise ValueError("Async run is missing its authoritative checkpoint manifest")
    elif any(path.name != "runner.lock" for path in directory.iterdir()):
        raise ValueError("Existing artifacts lack a resumable async identity; use a fresh output")


def write_snapshot(path: Path, payload: bytes, *, durable: bool) -> None:
    """Progress is replaceable; admission stops must flush before dispatch resumes."""
    if not durable:
        save(path, json.loads(payload))
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
