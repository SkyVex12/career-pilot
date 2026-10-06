"""Valkey state, events and log streams (ARCHITECTURE.md, "Valkey key design").

Writes for a live session go through `SessionWriter`, an in-memory buffer drained by one task:
- Every batch runs as one Lua script that first checks `sess:{sid}:lease` still names this
  gateway task. A task that lost the session to a resume elsewhere can therefore never write
  into it again (fencing); the writer reports the loss and the session stops.
- Server messages go to `sess:{sid}:events` with the stream id `<seq>-0`, so a resume replays
  with XRANGE from `<last_seq + 1>-0`, and Valkey itself rejects a seq that goes backwards.
- When Valkey is unavailable, the buffer keeps everything and the writer retries
  (failure row "Valkey unavailable"): the live session carries on from memory.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import valkey.asyncio as valkey_async
from valkey.asyncio.cluster import ValkeyCluster
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from copilot_core import keys
from copilot_core.packs import Digest, SessionPack
from copilot_gateway.config import Settings

log = logging.getLogger(__name__)

UNAVAILABLE = (ValkeyConnectionError, ValkeyTimeoutError, ConnectionError, OSError)

# valkey-py types its commands as "value or awaitable" for the sync and async clients alike,
# so the client is used untyped here and results are cast where they are read.
type Client = Any

APPEND_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return {0, ''} end
local p = cjson.decode(ARGV[2])
for _, e in ipairs(p.events) do
  redis.call('XADD', KEYS[2], 'MAXLEN', '~', p.maxlen, e[1] .. '-0', 'm', e[2])
end
local last = ''
for _, entry in ipairs(p.log) do
  last = redis.call('XADD', KEYS[3], '*', 'e', entry)
end
if #p.state > 0 then
  redis.call('HSET', KEYS[4], unpack(p.state))
end
if #p.events > 0 then redis.call('EXPIRE', KEYS[2], p.ttl) end
redis.call('EXPIRE', KEYS[4], p.ttl)
return {1, last}
"""

RENEW_LUA = """
local holder = redis.call('GET', KEYS[1])
if holder == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
if not holder then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 2
end
return 0
"""

EXPIRE_IF_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 0
"""

DEL_IF_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def make_client(settings: Settings) -> Client:
    opts: dict[str, Any] = {"socket_timeout": 1.0, "socket_connect_timeout": 1.0}
    if settings.valkey_cluster:
        return ValkeyCluster.from_url(settings.valkey_url, **opts)
    return valkey_async.Valkey.from_url(settings.valkey_url, **opts)


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class LeaseLostError(Exception):
    pass


class GatewayStore:
    """Valkey access shared by all sessions on this gateway task."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._append = client.register_script(APPEND_LUA)
        self._renew = client.register_script(RENEW_LUA)
        self._del_if = client.register_script(DEL_IF_LUA)
        self._expire_if = client.register_script(EXPIRE_IF_LUA)

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())  # pyright: ignore[reportUnknownMemberType]
        except UNAVAILABLE:
            return False

    # One live session per user.

    async def claim_active(self, uid: UUID, sid: UUID) -> str | None:
        """Claim the user's live-session slot; returns the other session id if it is taken."""
        key = keys.user_active_session(uid)
        ttl = self.settings.state_ttl_s
        if await self.client.set(key, str(sid), nx=True, ex=ttl):
            return None
        current = await self.client.get(key)
        if current is None:  # expired in between
            await self.client.set(key, str(sid), ex=ttl)
            return None
        return _s(current)

    async def replace_active(self, uid: UUID, old_sid: str, sid: UUID) -> bool:
        """Take the slot from a stale session, only if it still names that session."""
        key = keys.user_active_session(uid)
        if await self._del_if(keys=[key], args=[old_sid]):
            return bool(await self.client.set(key, str(sid), nx=True, ex=self.settings.state_ttl_s))
        return False

    async def refresh_active(self, uid: UUID, sid: UUID) -> None:
        """Extend the slot's TTL, only while it still names this session."""
        key = keys.user_active_session(uid)
        await self._expire_if(keys=[key], args=[str(sid), self.settings.state_ttl_s])

    async def set_active(self, uid: UUID, sid: UUID) -> None:
        await self.client.set(keys.user_active_session(uid), str(sid), ex=self.settings.state_ttl_s)

    async def release_active(self, uid: UUID, sid: UUID) -> None:
        await self._del_if(keys=[keys.user_active_session(uid)], args=[str(sid)])

    # Lease: the task holding the live connection.

    async def take_lease(self, sid: UUID, task_id: str) -> None:
        """Take the lease even from another task: the newest connection wins."""
        ttl_ms = int(self.settings.lease_ttl_s * 1000)
        await self.client.set(keys.sess_lease(sid), task_id, px=ttl_ms)

    async def lease_holder(self, sid: UUID | str) -> str | None:
        value = await self.client.get(keys.sess_lease(sid))
        return None if value is None else _s(value)

    async def renew_lease(self, sid: UUID, task_id: str) -> bool:
        """Renew, or retake after an outage let it expire; False when another task holds it."""
        ttl_ms = int(self.settings.lease_ttl_s * 1000)
        result = await self._renew(keys=[keys.sess_lease(sid)], args=[task_id, ttl_ms])
        return int(cast(int, result)) != 0

    async def release_lease(self, sid: UUID, task_id: str) -> None:
        await self._del_if(keys=[keys.sess_lease(sid)], args=[task_id])

    # Session state and events.

    async def load_state(self, sid: UUID | str) -> dict[str, str]:
        raw = await self.client.hgetall(keys.sess_state(sid))  # pyright: ignore[reportUnknownMemberType]
        return {_s(k): _s(v) for k, v in cast(dict[Any, Any], raw).items()}

    async def events_after(self, sid: UUID, seq: int) -> list[tuple[int, str]]:
        entries = await self.client.xrange(keys.sess_events(sid), min=f"{seq + 1}-0", max="+")
        out: list[tuple[int, str]] = []
        for entry_id, fields in cast(list[tuple[Any, dict[Any, Any]]], entries):
            message = fields.get(b"m", fields.get("m"))
            out.append((int(_s(entry_id).split("-")[0]), _s(message)))
        return out

    async def last_event_seq(self, sid: UUID) -> int:
        entries = await self.client.xrevrange(keys.sess_events(sid), count=1)
        entries = cast(list[tuple[Any, Any]], entries)
        return int(_s(entries[0][0]).split("-")[0]) if entries else 0

    async def append(
        self,
        sid: UUID,
        task_id: str,
        events: list[tuple[int, str]],
        log_entries: list[str],
        state: Mapping[str, str],
    ) -> str:
        """One fenced batch; returns the last log entry id written ('' if none)."""
        flat: list[str] = []
        for k, v in state.items():
            flat += [k, v]
        payload = json.dumps(
            {
                "events": [[str(seq), m] for seq, m in events],
                "log": log_entries,
                "state": flat,
                "maxlen": self.settings.events_maxlen,
                "ttl": self.settings.state_ttl_s,
            },
            separators=(",", ":"),
        )
        result = cast(
            list[Any],
            await self._append(
                keys=[
                    keys.sess_lease(sid),
                    keys.sess_events(sid),
                    keys.sess_log(sid),
                    keys.sess_state(sid),
                ],
                args=[task_id, payload],
            ),
        )
        if int(result[0]) == 0:
            raise LeaseLostError(str(sid))
        return _s(result[1])

    async def end_session(self, sid: UUID, task_id: str) -> None:
        await self.mark_ended(sid, "ended")
        await self.release_lease(sid, task_id)

    async def mark_ended(self, sid: UUID | str, reason: str) -> None:
        await self.client.hset(
            keys.sess_state(sid),
            mapping={"status": "ended", "ended_reason": reason, "updated_at": str(time.time())},
        )

    # Digest, packs and the exact-repeat cache.

    async def get_digest(self, uid: UUID) -> Digest | None:
        raw = await self.client.get(keys.digest(uid))
        return None if raw is None else Digest.model_validate_json(raw)

    async def get_pack(self, uid: UUID, job_id: UUID | None, mode: str) -> SessionPack | None:
        index_key = keys.pack_index(uid)
        pack_id = None
        if job_id is not None:
            pack_id = await self.client.hget(index_key, f"job:{job_id}")  # pyright: ignore[reportUnknownMemberType]
        if pack_id is None:
            pack_id = await self.client.hget(index_key, f"mode:{mode}")  # pyright: ignore[reportUnknownMemberType]
        if pack_id is None:
            return None
        return await self.get_pack_by_id(uid, _s(pack_id))

    async def pack_index(self, uid: UUID) -> dict[str, str]:
        raw = await self.client.hgetall(keys.pack_index(uid))  # pyright: ignore[reportUnknownMemberType]
        return {_s(k): _s(v) for k, v in cast(dict[Any, Any], raw).items()}

    async def get_pack_by_id(self, uid: UUID, pack_id: str) -> SessionPack | None:
        raw = await self.client.get(keys.pack(uid, pack_id))
        return None if raw is None else SessionPack.model_validate_json(raw)

    async def qa_get(self, uid: UUID, qhash: str) -> dict[str, Any] | None:
        raw = await self.client.get(keys.qa(uid, qhash))
        return None if raw is None else cast(dict[str, Any], json.loads(raw))

    async def qa_put(self, uid: UUID, qhash: str, value: Mapping[str, Any]) -> None:
        await self.client.set(keys.qa(uid, qhash), json.dumps(value), ex=7 * 24 * 3600)


@dataclass
class _Batch:
    events: list[tuple[int, str]]
    log: list[str]
    state: dict[str, str]


class SessionWriter:
    """Buffered, fenced writes for one session; see the module docstring."""

    def __init__(
        self,
        store: GatewayStore,
        sid: UUID,
        task_id: str,
        on_lost: Callable[[], None],
        on_availability: Callable[[bool], None] | None = None,
        buffer_max: int = 50_000,
    ) -> None:
        self.store = store
        self.sid = sid
        self.task_id = task_id
        self.on_lost = on_lost
        self.on_availability = on_availability
        self.buffer_max = buffer_max
        self.events: deque[tuple[int, str]] = deque()
        self.log: deque[str] = deque()
        self.state: dict[str, str] = {}
        self.available = True
        self.last_log_id = ""
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped_events = 0

    def event(self, seq: int, message: str) -> None:
        self.events.append((seq, message))
        while len(self.events) > self.buffer_max:  # an outage that long loses the oldest
            self.events.popleft()
            self.dropped_events += 1
        self._kick()

    def log_entry(self, entry: Mapping[str, Any]) -> None:
        self.log.append(json.dumps(entry, separators=(",", ":"), default=str))
        self._kick()

    def set_state(self, **fields: object) -> None:
        for k, v in fields.items():
            self.state[k] = v if isinstance(v, str) else json.dumps(v, default=str)
        self._kick()

    def _kick(self) -> None:
        self._idle.clear()
        self._wake.set()

    def pending(self) -> bool:
        return bool(self.events or self.log or self.state)

    async def drain(self, within_s: float) -> bool:
        """Wait until everything buffered is written; False on timeout."""
        try:
            await asyncio.wait_for(self._idle.wait(), within_s)
            return True
        except TimeoutError:
            return False

    async def run(self) -> None:
        backoff = 0.1
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.pending():
                batch = _Batch(
                    events=[self.events[i] for i in range(min(len(self.events), 200))],
                    log=[self.log[i] for i in range(min(len(self.log), 200))],
                    state=dict(self.state),
                )
                try:
                    last = await self.store.append(
                        self.sid, self.task_id, batch.events, batch.log, batch.state
                    )
                except LeaseLostError:
                    log.warning("session %s: lease lost, stopping writes", self.sid)
                    self.on_lost()
                    return
                except UNAVAILABLE as e:
                    if self.available:
                        log.warning("valkey unavailable (%s); buffering session %s", e, self.sid)
                        self.available = False
                        if self.on_availability:
                            self.on_availability(False)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                    continue
                backoff = 0.1
                if not self.available:
                    self.available = True
                    log.info("valkey back; session %s buffer flushed", self.sid)
                    if self.on_availability:
                        self.on_availability(True)
                for _ in batch.events:
                    self.events.popleft()
                for _ in batch.log:
                    self.log.popleft()
                for k, v in batch.state.items():
                    if self.state.get(k) == v:
                        del self.state[k]
                if last:
                    self.last_log_id = last
            self._idle.set()
