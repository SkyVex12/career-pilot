"""Private immutable source snapshots and conservative replay resume checkpoints."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import sys
import zipfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

from copilot_evals.sensei_runtime import save


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")


def source_archive(
    directory: Path,
    root: Path,
    code: dict[str, str],
    safe_configuration: dict[str, Any],
    prompt_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Archive exactly hashed Python sources and explicit safe settings, never environment files."""
    root = root.resolve()
    sources: dict[str, bytes] = {}
    for relative, expected in sorted(code.items()):
        path = (root / relative).resolve()
        if root not in path.parents or path.suffix != ".py":
            raise ValueError("Source archive accepts only Python files inside the repository")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("Source changed during identity capture; retry with stable sources")
        sources[relative] = content
    dependencies = {
        "python": sys.version,
        "packages": sorted(
            {
                (distribution.metadata["Name"], distribution.version)
                for distribution in importlib.metadata.distributions()
                if distribution.metadata["Name"]
            }
        ),
        "scope": "Installed package names/versions, not wheel binaries or environment secrets",
    }
    sources["dependency-versions.json"] = _bytes(dependencies)
    sources["safe-configuration.json"] = _bytes(safe_configuration)
    prompt_manifest: dict[str, dict[str, str]] = {}
    for label, text in sorted((prompt_sources or {}).items()):
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"prompt-snapshots/{digest}.txt"
        sources[name] = encoded
        prompt_manifest[label] = {"filename": name, "sha256": digest}
    sources["prompt-snapshots.json"] = _bytes(prompt_manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(sources.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, content)
    content = buffer.getvalue()
    target = directory / "source-archive.zip"
    if target.exists() and target.read_bytes() != content:
        raise ValueError("Immutable source archive differs; use a fresh output directory")
    if not target.exists():
        target.write_bytes(content)
    for name in ["prompt-snapshots.json", *{item["filename"] for item in prompt_manifest.values()}]:
        snapshot = directory / name
        if snapshot.exists() and snapshot.read_bytes() != sources[name]:
            raise ValueError("Immutable prompt snapshot changed; use a fresh output directory")
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(sources[name])
    dependency_path = directory / "dependency-versions.json"
    if dependency_path.exists() and dependency_path.read_bytes() != sources[dependency_path.name]:
        raise ValueError("Dependency versions changed; use a fresh output directory")
    if not dependency_path.exists():
        dependency_path.write_bytes(sources[dependency_path.name])
    return {
        "filename": target.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "python_source_files": len(code),
        "prompt_snapshots": prompt_manifest,
        "dependency_manifest_sha256": hashlib.sha256(sources[dependency_path.name]).hexdigest(),
        "scope": "Exact source/config snapshot; dependencies are identified but not vendored",
    }


class DispatchJournal:
    """A prior started request without a result is ambiguous and is never redispatched."""

    def __init__(
        self,
        directory: Path,
        planned: list[str],
        completed: set[str],
        attempted: set[str] | None = None,
    ):
        self.path = directory / "dispatch-state.json"
        if len(set(planned)) != len(planned):
            raise ValueError("Duplicate planned dispatch identity")
        self.planned = planned
        previous: dict[str, Any] = (
            json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        )
        if previous and previous["planned"] != planned:
            raise ValueError("Dispatch schedule changed; use a fresh output directory")
        prior = previous.get("states", {})
        self.states: dict[str, str] = {}
        for identifier in planned:
            self.states[identifier] = (
                "completed"
                if identifier in completed
                else "ambiguous"
                if prior.get(identifier) in ("in_flight", "ambiguous", "completed")
                or identifier in (attempted or set())
                else "not_started"
            )
        self.checkpoint()

    def checkpoint(self) -> None:
        save(
            self.path,
            {
                "schema": 1,
                "planned": self.planned,
                "states": self.states,
                "remaining": [
                    key for key, status in self.states.items() if status == "not_started"
                ],
                "in_flight": [key for key, status in self.states.items() if status == "in_flight"],
                "ambiguous": [key for key, status in self.states.items() if status == "ambiguous"],
                "policy": "Never redispatch a prior started request lacking a durable result",
            },
        )

    def start(self, identifier: str) -> bool:
        if self.states[identifier] != "not_started":
            return False
        self.states[identifier] = "in_flight"
        self.checkpoint()
        return True

    def finish(self, identifier: str) -> None:
        if self.states[identifier] != "in_flight":
            raise ValueError("Cannot finish a dispatch that is not in flight")
        self.states[identifier] = "completed"
        self.checkpoint()


def immutable(path: Path, value: Any) -> None:
    if path.exists():
        if json.loads(path.read_text("utf-8")) != value:
            raise ValueError("Immutable checkpoint mismatch")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, value)


@contextlib.contextmanager
def run_lock(directory: Path) -> Generator[None]:
    """OS lock releases on process death; runners cannot race the shared ledger."""
    with (directory / "runner.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        lock = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")
        try:
            if os.name == "nt":
                lock.locking(handle.fileno(), lock.LK_NBLCK, 1)
            else:
                lock.flock(handle.fileno(), lock.LOCK_EX | lock.LOCK_NB)
        except OSError as error:
            raise ValueError("Another runner owns this output directory") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                lock.locking(handle.fileno(), lock.LK_UNLCK, 1)
            else:
                lock.flock(handle.fileno(), lock.LOCK_UN)
