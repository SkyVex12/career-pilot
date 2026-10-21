"""Logging for every service: one line per record, with the session id on each line.

`configure(service)` replaces `logging.basicConfig` in the entry points. Each record carries
the ids bound in the current context (`bind(session_id=..., user_id=..., job=...)`), so a
line logged anywhere under a session, a request or a job names it without the call site
passing it along. asyncio tasks copy the context when they are created, so a task started
inside a bound block keeps the ids.

- `COPILOT_LOG_FORMAT=json` writes JSON lines (the default on AWS and Compute Engine, where
  CloudWatch Logs Insights and Cloud Logging filter on `session_id`); `text` is the local
  default, with the ids appended.
- `COPILOT_LOG_DIR` also writes JSON lines to `<dir>/<service>-<start time>.log`, one file
  per run, for analyzing a local test the way CloudWatch allows in AWS.

Only ids, timings, counts and errors go into logs, never transcript or answer text
(ARCHITECTURE.md, "Observability and latency budget").
"""

import contextlib
import contextvars
import json
import logging
import os
import sys
from collections.abc import Generator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot_core.config import cloud_runtime

FIELDS = ("session_id", "user_id", "job", "request_id")

_context: contextvars.ContextVar[Mapping[str, str] | None] = contextvars.ContextVar(
    "copilot_log_context", default=None
)


def current() -> Mapping[str, str]:
    return _context.get() or {}


def set_ids(**ids: object) -> None:
    """Bind ids for the rest of the current context (a task, a request). None values unbind."""
    merged = {**current()}
    for key, value in ids.items():
        if key not in FIELDS:
            raise ValueError(f"unknown log field {key!r}")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    _context.set(merged)


@contextlib.contextmanager
def bind(**ids: object) -> Generator[None]:
    """Bind ids for the duration of a block."""
    token = _context.set(_context.get())
    try:
        set_ids(**ids)
        yield
    finally:
        _context.reset(token)


class ContextFilter(logging.Filter):
    """Copies the bound ids onto every record, so formatters and handlers see them."""

    def filter(self, record: logging.LogRecord) -> bool:
        ids = current()
        for key in FIELDS:
            if not hasattr(record, key):
                setattr(record, key, ids.get(key))
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            # Cloud Logging reads the entry's severity from this key; CloudWatch ignores it.
            "severity": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            # Always present, null outside a session, so a CloudWatch query on it is uniform.
            "session_id": getattr(record, "session_id", None),
        }
        for key in FIELDS[1:]:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ids = " ".join(
            f"{key}={value}" for key in FIELDS if (value := getattr(record, key, None)) is not None
        )
        return f"{line} [{ids}]" if ids else line


def configure(service: str, environ: Mapping[str, str] | None = None) -> Path | None:
    """Set up the root logger for a service; returns the log file, if one was opened."""
    env = os.environ if environ is None else environ
    fmt = env.get("COPILOT_LOG_FORMAT") or ("json" if cloud_runtime(env) else "text")
    level = env.get("LOG_LEVEL", "INFO")
    context = ContextFilter()

    console = logging.StreamHandler(sys.stdout if fmt == "json" else sys.stderr)
    console.setFormatter(JsonFormatter(service) if fmt == "json" else TextFormatter())
    console.addFilter(context)
    handlers: list[logging.Handler] = [console]

    path: Path | None = None
    if log_dir := env.get("COPILOT_LOG_DIR"):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = Path(log_dir) / f"{service}-{stamp}-{os.getpid()}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        file = logging.FileHandler(path, encoding="utf-8")
        file.setFormatter(JsonFormatter(service))
        file.addFilter(context)
        handlers.append(file)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    # uvicorn's loggers go through the root handlers instead of their own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    return path
