"""Request-local snapshot used only by allowlisted, side-effect-free reads."""

from contextvars import ContextVar
from dataclasses import dataclass

from copilot_core.auth import Principal
from copilot_core.db import Conn


@dataclass
class ReadSnapshot:
    account: Principal
    user: Principal
    authorization: str | None
    profile: str | None
    conn: Conn


snapshot: ContextVar[ReadSnapshot | None] = ContextVar("cache_read_snapshot", default=None)
