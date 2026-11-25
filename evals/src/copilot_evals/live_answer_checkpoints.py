"""Small atomic replay records; aggregate exports are written only after workers drain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from copilot_evals.sensei_runtime import save

POLICY = "atomic-records-v1"


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


class RecordStore:
    """Single-process, OS-locked runner storage. Final JSON arrays are not authoritative.

    A record is flushed before its atomic rename. A leftover temporary file represents
    an uncommitted transition and is ignored; malformed committed records fail closed.
    As with ordinary local files, machine/power-loss durability depends on the filesystem.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.root = directory / "checkpoint-records"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, dict[str, int]] = {}
        self.write_samples: list[dict[str, Any]] = []
        self.origin = time.perf_counter()
        self.phase = "startup"
        self.on_error: Callable[[], None] | None = None
        manifest = {"schema": 1, "policy": POLICY, "record_encoding": "compact-json-sha256"}
        path = self.root / "manifest.json"
        if path.exists():
            if json.loads(path.read_text("utf-8")) != manifest:
                raise ValueError("Unsupported checkpoint record manifest")
        elif any(self.root.iterdir()):
            raise ValueError("Checkpoint records lack their version manifest")
        else:
            save(path, manifest)

    def _folder(self, kind: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z-]{0,39}", kind):
            raise ValueError("Invalid checkpoint kind")
        return self.root / kind

    def read(self, kind: str) -> list[Any]:
        rows: list[tuple[int, str, Any]] = []
        folder = self._folder(kind)
        for path in sorted(folder.glob("*.json")):
            try:
                item = json.loads(path.read_text("utf-8"))
                ordinal, key, value = item["ordinal"], item["key"], item["value"]
                if (
                    item["schema"] != 1
                    or type(ordinal) is not int
                    or ordinal < 0
                    or not isinstance(key, str)
                    or item["sha256"] != hashlib.sha256(_encode(value)).hexdigest()
                    or path.name != self._filename(ordinal, key)
                ):
                    raise ValueError("Invalid record")
                rows.append((ordinal, key, value))
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError("Corrupt committed checkpoint record; refusing resume") from error
        if [row[0] for row in rows] != list(range(len(rows))) or len({r[1] for r in rows}) != len(
            rows
        ):
            raise ValueError("Missing or duplicate checkpoint record; refusing resume")
        self.index[kind] = {key: ordinal for ordinal, key, _ in rows}
        return [value for _, _, value in rows]

    @staticmethod
    def _filename(ordinal: int, key: str) -> str:
        return f"{ordinal:09d}-{hashlib.sha256(key.encode()).hexdigest()}.json"

    def put(self, kind: str, key: str, value: Any) -> None:
        """Persist one changed record, with work proportional to this record alone."""
        started = time.perf_counter()
        try:
            if kind not in self.index:
                self.read(kind)
            index = self.index[kind]
            ordinal = index.get(key, len(index))
            folder = self._folder(kind)
            folder.mkdir(exist_ok=True)
            path = folder / self._filename(ordinal, key)
            temporary = path.with_suffix(".tmp")
            payload = _encode(value)
            prefix = _encode(
                {
                    "schema": 1,
                    "ordinal": ordinal,
                    "key": key,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )[:-1]
            encoded = prefix + b',"value":' + payload + b"}"
            serialized = time.perf_counter()
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            finished = time.perf_counter()
            index[key] = ordinal
            self.write_samples.append(
                {
                    "kind": kind,
                    "phase": self.phase,
                    "at_s": started - self.origin,
                    "bytes": len(encoded),
                    "serialize_ms": (serialized - started) * 1000,
                    "io_ms": (finished - serialized) * 1000,
                    "total_ms": (finished - started) * 1000,
                }
            )
        except BaseException:
            if self.on_error is not None:
                self.on_error()
            raise

    def export(self, kind: str, filename: str) -> None:
        """Only call after generation has drained; full arrays keep existing readers compatible."""
        save(self.directory / filename, self.read(kind))

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "policy": POLICY,
            "writes": self.write_samples,
            "scope": (
                "Synchronous individual record serialization, flush/fsync and rename; "
                "no latency subtraction"
            ),
        }


class RecordDispatchJournal:
    """A durable start without a durable result remains ambiguous, never redispatched."""

    def __init__(
        self, store: RecordStore, planned: list[str], completed: set[str], attempted: set[str]
    ):
        self.store, self.planned = store, planned
        if len(set(planned)) != len(planned):
            raise ValueError("Duplicate planned dispatch identity")
        if (completed | attempted) - set(planned):
            raise ValueError("Unexpected dispatch identity in persisted results or attempts")
        existing = store.read("dispatch-plan")
        if existing and existing != [planned]:
            raise ValueError("Dispatch schedule changed; use a fresh output directory")
        if not existing:
            store.put("dispatch-plan", "planned", planned)
        prior = {row["id"]: row["state"] for row in store.read("dispatch")}
        if set(prior) - set(planned) or any(
            value not in {"in_flight", "completed", "ambiguous"} for value in prior.values()
        ):
            raise ValueError("Invalid persisted dispatch state")
        self.states = {
            identifier: "completed"
            if identifier in completed
            else "ambiguous"
            if identifier in prior or identifier in attempted
            else "not_started"
            for identifier in planned
        }
        self.dirty = {
            key
            for key, state in self.states.items()
            if state != "not_started" and prior.get(key) != state
        }
        self.checkpoint()

    def checkpoint(self) -> None:
        for key in sorted(self.dirty):
            self.store.put("dispatch", key, {"id": key, "state": self.states[key]})
            self.dirty.remove(key)

    def start(self, identifier: str) -> bool:
        if self.states[identifier] != "not_started":
            return False
        self.states[identifier] = "in_flight"
        self.dirty.add(identifier)
        self.checkpoint()
        return True

    def finish(self, identifier: str) -> None:
        if self.states[identifier] != "in_flight":
            raise ValueError("Cannot finish a dispatch that is not in flight")
        self.states[identifier] = "completed"
        self.dirty.add(identifier)
        self.checkpoint()

    def export(self) -> None:
        save(
            self.store.directory / "dispatch-state.json",
            {
                "schema": 1,
                "planned": self.planned,
                "states": self.states,
                "remaining": [key for key, state in self.states.items() if state == "not_started"],
                "in_flight": [key for key, state in self.states.items() if state == "in_flight"],
                "ambiguous": [key for key, state in self.states.items() if state == "ambiguous"],
                "policy": "Never redispatch a prior started request lacking a durable result",
            },
        )


class LoopLagMonitor:
    """Bounded all-sample histogram; wall time is observed, never corrected away."""

    def __init__(self, interval_s: float = 0.05):
        self.interval_s = interval_s
        self.histogram: Counter[int] = Counter()
        self.maximum_ms = self.total_ms = 0.0
        self.samples = 0
        self.task: asyncio.Task[None] | None = None
        self.next_due: float | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0)

    def observe(self, lag: float) -> None:
        self.samples += 1
        self.total_ms += lag
        self.maximum_ms = max(self.maximum_ms, lag)
        self.histogram[min(10000, int(lag))] += 1

    async def _run(self) -> None:
        while True:
            self.next_due = time.perf_counter() + self.interval_s
            await asyncio.sleep(self.interval_s)
            self.observe(max(0.0, (time.perf_counter() - self.next_due) * 1000))
            self.next_due = None

    async def stop(self) -> None:
        # Capture an overdue final tick before cancellation, without adding a tail sleep
        # or losing the last blocking write to timer/task callback scheduling order.
        now = time.perf_counter()
        if self.next_due is not None and now >= self.next_due:
            self.observe((now - self.next_due) * 1000)
            self.next_due = None
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def summary(self) -> dict[str, Any]:
        def percentile(q: float) -> list[int] | None:
            rank = math.ceil(self.samples * q)
            cumulative = 0
            for bucket, count in sorted(self.histogram.items()):
                cumulative += count
                if cumulative >= rank:
                    return [bucket, bucket + 1] if bucket < 10000 else None
            return None

        return {
            "schema": 1,
            "interval_ms": self.interval_s * 1000,
            "samples": self.samples,
            "p50_ms_interval": percentile(0.5),
            "p90_ms_interval": percentile(0.9),
            "p95_ms_interval": percentile(0.95),
            "max_ms": self.maximum_ms,
            "mean_ms": self.total_ms / self.samples if self.samples else None,
            "histogram_ms": dict(sorted(self.histogram.items())),
            "overflow_ge_10000_ms": self.histogram[10000],
            "scope": (
                "Active workers only; one-ms histogram; final aggregate exports excluded; "
                "no latency subtraction"
            ),
        }
