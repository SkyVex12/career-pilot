"""The seat relay: guests' connections, on `/v1/seat` (ARCHITECTURE.md, "Guest seats").

A guest's connection never goes near the host's: no `attach`, no lease, no active-session
slot, no minutes, no STT. It may land on any gateway task, whichever one holds the call.

- **Join.** The first message is `seat.join`. The seat must be the guest's, admitted, of a
  call that is still live, in the Valkey mirror and, with a database, in `app.session_seats`
  too. A newer connection for the same seat takes it over (4410 to the older one).
- **Snapshot, then live.** The guest gets `seat.ready`, then the call so far as one
  `seat.snapshot` (the host's view, `sess:{sid}:view`, plus the events after it), then every
  later host message that `seatview.guest_message` lets through, in seq order, with no gap and
  no duplicate. A guest who falls `seat_queue_max` messages behind gets a fresh snapshot
  instead of an ever longer queue; nobody else waits for them.
- **One feed per call per task.** `SessionFeed` makes one blocking read of the call's event
  and result streams for every guest of that call on this task, on the store's blocking
  client, and checks the seat list and the call's state once a second: a removed guest is
  closed (4411), a narrower grant is sent (`seat.update`), the host's connection is reported
  (`seat.host`), and the end of the call closes every guest (1000).
- **Commands.** `seat.command` is checked here (grant, size, rate) and queued on
  `sess:{sid}:ctl` for the host's session, which checks it again against the grant as it
  stands then and answers on `sess:{sid}:ctlres`.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg
from pydantic import ValidationError
from starlette.websockets import WebSocket

from copilot_core import careeros, logs, seats, versions
from copilot_core.db import user_tx
from copilot_core.ids import uuid7
from copilot_gateway.auth import AuthError, Principal, bearer_token
from copilot_gateway.seatview import ViewFold, guest_message, parse, sharing_of
from copilot_gateway.store import UNAVAILABLE
from copilot_protocol import models

if TYPE_CHECKING:
    from copilot_gateway.session import Services, SessionManager

log = logging.getLogger(__name__)

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_UNAUTHORIZED = 4401
CLOSE_NO_START = 4408
CLOSE_REPLACED = 4410
CLOSE_SEAT_REMOVED = 4411
CLOSE_SEAT_CLOSED = 4412
CLOSE_INTERNAL = 4500

MAX_GUEST_JSON = 8 * 1024  # a note is at most 1,000 characters


def _dump(message: Any) -> str:
    return message.model_dump_json(exclude_none=True, by_alias=True)


class SessionFeed:
    """One call's event and result streams, read once for every guest of it on this task."""

    def __init__(self, relay: "SeatRelay", sid: str) -> None:
        self.relay = relay
        self.sid = sid
        self.store = relay.svc.store
        self.settings = relay.svc.settings
        self.guests: set[GuestConn] = set()
        self.ready = asyncio.Event()  # the read position is set: catch-ups may start
        self.task: asyncio.Task[None] | None = None
        self.state: dict[str, str] = {}
        self.records: dict[str, seats.SeatRecord] = {}

    def add(self, guest: "GuestConn") -> None:
        self.guests.add(guest)
        if self.task is None or self.task.done():
            self.ready.clear()
            self.task = asyncio.create_task(self._run())

    def remove(self, guest: "GuestConn") -> None:
        self.guests.discard(guest)
        if not self.guests:
            self.relay.feeds.pop(self.sid, None)
            if self.task is not None:
                self.task.cancel()

    async def _run(self) -> None:
        s = self.settings
        events_id = results_id = ""
        checked = 0.0
        backoff = 0.2
        while self.guests:
            try:
                if not events_id:
                    events_id, results_id = await self.store.stream_tail(self.sid)
                    await self.refresh()
                    checked = time.monotonic()
                    self.ready.set()
                events, results = await self.store.read_feed(
                    self.sid, events_id, results_id, s.seat_block_ms
                )
                for entry_id, data in events:
                    events_id = entry_id
                    seq = int(entry_id.split("-")[0])
                    for g in list(self.guests):
                        g.offer(seq, data)
                for entry_id, f in results:
                    results_id = entry_id
                    for g in list(self.guests):
                        if g.seat_id == f.get("s"):
                            g.result(f.get("c", ""), f.get("r", "failed"))
                if time.monotonic() - checked >= s.seat_poll_s:
                    await self.refresh()
                    checked = time.monotonic()
                    for g in list(self.guests):
                        await g.check()
                backoff = 0.2
            except asyncio.CancelledError:
                raise
            except UNAVAILABLE as e:
                log.info("seat feed %s: valkey unavailable (%s)", self.sid, type(e).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            except Exception:
                log.exception("seat feed %s failed; retrying", self.sid)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def refresh(self) -> None:
        self.state = await self.store.load_state(self.sid)
        self.records = await seats.read_all(self.store.client, self.sid)


class GuestConn:
    def __init__(
        self,
        relay: "SeatRelay",
        ws: WebSocket,
        principal: Principal,
        sid: str,
        seat: seats.SeatRecord,
        client_version: str,
    ) -> None:
        self.relay = relay
        self.ws = ws
        self.principal = principal
        self.sid = sid
        self.seat_id = seat.seat_id
        self.record = seat
        self.client_version = client_version
        self.conn_id = str(uuid7())
        self.settings = relay.svc.settings
        self.store = relay.svc.store
        self.out: asyncio.Queue[str | None] = asyncio.Queue()
        self.last_seq = 0
        self.sharing = True
        self.host_connected = True
        self.synced = False  # the snapshot and its catch-up are out: live messages may follow
        self.early: list[tuple[int, str]] = []
        self.need_reset = False
        self.closing: tuple[int, str] | None = None
        self.notes_at: deque[float] = deque(maxlen=max(1, self.settings.seat_notes_per_minute))
        self.sent = 0
        self.resets = 0
        self.notes = 0

    @property
    def pos(self) -> int:
        return max(1, self.last_seq)

    # From the feed.

    def offer(self, seq: int, data: str) -> None:
        if not self.synced:
            self.early.append((seq, data))
            return
        self._forward(seq, data)

    def _forward(self, seq: int, data: str) -> None:
        if seq <= self.last_seq:
            return
        self.last_seq = seq
        message = parse(data)
        if message is None:
            return
        sharing = sharing_of(message)
        if sharing is not None:
            if sharing != self.sharing:
                self.sharing = sharing
                self._host()
            return
        if not self.sharing:
            return
        out = guest_message(data)
        if out is not None:
            self._push(out)

    def _push(self, data: str) -> None:
        if self.out.qsize() >= self.settings.seat_queue_max:
            self.need_reset = True
            return
        self.out.put_nowait(data)

    def _send(self, message: Any) -> None:
        self.out.put_nowait(_dump(message))

    def _host(self) -> None:
        self._send(
            models.SeatHost(
                type="seat.host", seq=self.pos, connected=self.host_connected, sharing=self.sharing
            )
        )

    def result(self, command_id: str, result: str) -> None:
        try:
            cid = UUID(command_id)
        except ValueError:
            return
        self._send(
            models.SeatCommandResult.model_validate(
                {
                    "type": "seat.command.result",
                    "seq": self.pos,
                    "command_id": cid,
                    "result": result,
                }
            )
        )

    async def check(self) -> None:
        """Once a second: the seat, the call, the connection's ownership, the token."""
        feed = self.relay.feeds.get(self.sid)
        if feed is None:
            return
        state, record = feed.state, feed.records.get(self.seat_id)
        if not state or state.get("status") == "ended":
            self.close(CLOSE_NORMAL, "the call ended")
            return
        if record is None or record.guest_id != str(self.principal.user_id):
            self.close(CLOSE_SEAT_CLOSED, "the seat is closed")
            return
        if record.status == "removed":
            self.close(CLOSE_SEAT_REMOVED, "removed by the host")
            return
        if not record.admitted:
            self.close(CLOSE_SEAT_CLOSED, "the seat is closed")
            return
        if record.rev != self.record.rev and record.capabilities != self.record.capabilities:
            self._send(
                models.SeatUpdate.model_validate(
                    {"type": "seat.update", "seq": self.pos, "capabilities": record.capabilities}
                )
            )
        self.record = record
        connected = state.get("status") == "active"
        if connected != self.host_connected:
            self.host_connected = connected
            self._host()
        if await self.store.seat_conn(self.seat_id) not in (None, self.conn_id):
            self.close(CLOSE_REPLACED, "replaced by a newer connection")
            return
        expires_at = self.principal.expires_at
        if expires_at is not None and expires_at < time.time():
            self.close(CLOSE_UNAUTHORIZED, "token expired")
            return
        if self.need_reset:
            await self.sync()

    # The snapshot.

    async def sync(self) -> None:
        """The call so far, then everything after it: on join, and when too far behind."""
        if self.last_seq:
            self.resets += 1
        self.synced, self.early, self.need_reset = False, [], False
        # The call's messages still queued are replaced by the snapshot; the seat's own
        # (seat.ready, results, updates) and a close stay, in order.
        kept: list[str | None] = []
        while not self.out.empty():
            data = self.out.get_nowait()
            if data is None or (parse(data) or {}).get("type", "").startswith("seat."):
                kept.append(data)
        for data in kept:
            self.out.put_nowait(data)
        feed = self.relay.feeds.get(self.sid)
        if feed is not None:
            await feed.ready.wait()
        fold = await self._view()
        self.sharing = fold.sharing
        for seq, data in await self.store.events_after(UUID(self.sid), fold.seq):
            fold.add(seq, data)
        self.last_seq = fold.seq
        self._send(
            models.SeatSnapshot.model_validate(
                {"type": "seat.snapshot", "seq": self.pos, "messages": fold.messages()}
            )
        )
        self._host()
        for seq, data in self.early:
            self._forward(seq, data)
        self.early, self.synced = [], True

    async def _view(self) -> ViewFold:
        deadline = time.monotonic() + self.settings.seat_view_wait_s
        while True:
            fold = ViewFold.load(await self.store.get_view(self.sid))
            if fold is not None:
                return fold
            if time.monotonic() >= deadline:
                # The host's session has not written one yet: fold the stream (its last
                # ~2,000 events) here instead.
                return ViewFold(
                    sharing=(await self.store.load_state(self.sid)).get("sharing") != "0"
                )
            await asyncio.sleep(0.1)

    # The connection.

    async def run(self, ready: dict[str, Any]) -> None:
        feed = self.relay.feed_for(self.sid)
        feed.add(self)
        sender = asyncio.create_task(self._sender())
        reader = asyncio.create_task(self._reader())
        try:
            await self.store.claim_seat_conn(self.seat_id, self.conn_id)
            await self._tell_host()  # the host's list shows the guest as watching
            self.host_connected = ready["host_connected"]
            self._send(models.SeatReady.model_validate({**ready, "seq": 1}))
            await self.sync()
            await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            feed.remove(self)
            self.relay.guests.discard(self)
            for task in (sender, reader):
                task.cancel()
            await asyncio.gather(sender, reader, return_exceptions=True)
            code, reason = self.closing or (CLOSE_NORMAL, "")
            with contextlib.suppress(Exception):
                await self.ws.close(code, reason)
            with contextlib.suppress(*UNAVAILABLE):
                await self.store.release_seat_conn(self.seat_id, self.conn_id)
                await self._tell_host()
            log.info(
                "seat closed: code=%s reason=%s sent=%d resets=%d notes=%d",
                code, reason, self.sent, self.resets, self.notes,
            )  # fmt: skip

    async def _tell_host(self) -> None:
        """The seat list changed for the host: this guest started or stopped watching."""
        with contextlib.suppress(*UNAVAILABLE):
            await self.store.add_ctl(self.sid, {"k": seats.CTL_SEATS})

    def close(self, code: int, reason: str) -> None:
        if self.closing is None:
            self.closing = (code, reason)
            self.out.put_nowait(None)

    async def _sender(self) -> None:
        while True:
            data = await self.out.get()
            if data is None:
                return
            await self.ws.send_text(data)
            self.sent += 1

    async def _reader(self) -> None:
        while True:
            raw = await self.ws.receive()
            if raw["type"] == "websocket.disconnect":
                return
            text: str | None = raw.get("text")
            if text is None:
                self._error("bad_frame", "a seat sends no audio")
                continue
            if len(text) > MAX_GUEST_JSON:
                self._error("message_too_large", f"messages are at most {MAX_GUEST_JSON} bytes")
                continue
            try:
                message = models.ClientMessage.model_validate_json(text).root
            except ValidationError as e:
                errors = e.errors(include_url=False)
                self._error("bad_message", errors[0]["msg"] if errors else "invalid")
                continue
            match message:
                case models.SeatCommand():
                    await self._command(message)
                case models.AuthRefresh():
                    await self._refresh(message.access_token)
                case _:
                    self._error("unexpected_message", f"{message.type} is not for a seat")

    def _error(self, code: str, message: str) -> None:
        self._send(
            models.ServerError(type="error", seq=self.pos, code=code, message=message, fatal=False)
        )

    async def _command(self, message: models.SeatCommand) -> None:
        cid = str(message.command_id)
        if not self.record.may(message.kind):
            self.result(cid, "denied")
            return
        now = time.monotonic()
        s = self.settings
        recent = [t for t in self.notes_at if now - t < 60]
        if (self.notes_at and now - self.notes_at[-1] < s.seat_note_min_interval_s) or len(
            recent
        ) >= s.seat_notes_per_minute:
            self.result(cid, "rate_limited")
            return
        self.notes_at.append(now)
        body = {
            "command_id": cid,
            "seat_id": self.seat_id,
            "guest_id": str(self.principal.user_id),
            "text": message.text,
            "expires_at": time.time() + s.seat_note_ttl_s,
        }
        try:
            await self.store.add_ctl(
                self.sid, {"k": seats.CTL_NOTE, "d": json.dumps(body, separators=(",", ":"))}
            )
            self.notes += 1
        except UNAVAILABLE:
            self.result(cid, "failed")

    async def _refresh(self, token: str) -> None:
        try:
            principal = await self.relay.svc.verifier.verify(token)
        except AuthError:
            self.close(CLOSE_UNAUTHORIZED, "unauthorized")
            return
        if principal.user_id != self.principal.user_id:
            self.close(CLOSE_UNAUTHORIZED, "the token is another account's")
            return
        self.principal = principal


class SeatRelay:
    def __init__(self, svc: "Services", manager: "SessionManager") -> None:
        self.svc = svc
        self.manager = manager
        self.feeds: dict[str, SessionFeed] = {}
        self.guests: set[GuestConn] = set()

    def feed_for(self, sid: str) -> SessionFeed:
        feed = self.feeds.get(sid)
        if feed is None:
            feed = self.feeds[sid] = SessionFeed(self, sid)
        return feed

    async def handle(self, ws: WebSocket) -> None:
        s = self.svc.settings
        token = bearer_token(ws.headers.get("authorization"))
        await ws.accept()
        reject = self.manager.reject
        try:
            principal = await self.svc.verifier.verify(token or "")
        except AuthError as e:
            await reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", str(e))
            return
        if s.require_career_os and not await self.manager.linked(principal):
            await reject(ws, CLOSE_REPLACED, careeros.REQUIRED_CODE, careeros.REQUIRED_MESSAGE)
            return
        logs.set_ids(user_id=principal.user_id)
        if not s.seats_enabled:
            await reject(ws, CLOSE_SEAT_CLOSED, "seats_off", "guest seats are turned off")
            return
        try:
            raw = await asyncio.wait_for(ws.receive(), s.first_message_timeout_s)
        except TimeoutError:
            await reject(ws, CLOSE_NO_START, "no_start", "send seat.join first")
            return
        if raw["type"] == "websocket.disconnect":
            return
        try:
            first = models.ClientMessage.model_validate_json(raw.get("text") or "").root
        except ValidationError:
            first = None
        if not isinstance(first, models.SeatJoin):
            await reject(ws, CLOSE_NO_START, "no_start", "the first message must be seat.join")
            return
        if versions.is_outdated(first.client_version, s.min_client_version):
            await self.manager.reject_outdated(ws, first.client_version)
            return
        sid, seat_id = str(first.session_id), str(first.seat_id)
        logs.set_ids(session_id=first.session_id)
        try:
            ready = await self._admit(principal, sid, seat_id)
        except UNAVAILABLE:
            await reject(
                ws, CLOSE_INTERNAL, "unavailable", "the call's state is unavailable; retry"
            )
            return
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("could not check the seat in the database: %s", e)
            await reject(ws, CLOSE_INTERNAL, "unavailable", "the seat cannot be checked; retry")
            return
        if isinstance(ready, str):
            log.info("seat refused: %s", ready)
            await reject(ws, CLOSE_SEAT_CLOSED, "seat_closed", ready)
            return
        record, body = ready
        log.info("seat joined: seat=%s from seq %d", seat_id, first.last_seq)
        guest = GuestConn(self, ws, principal, sid, record, first.client_version)
        self.guests.add(guest)
        await guest.run(body)

    async def _admit(
        self, principal: Principal, sid: str, seat_id: str
    ) -> tuple[seats.SeatRecord, dict[str, Any]] | str:
        """The seat and the seat.ready body, or why the guest may not join."""
        store = self.svc.store
        state = await store.load_state(sid)
        if not state or state.get("status") == "ended":
            return "the call is not live"
        records = await seats.read_all(store.client, sid)
        record = records.get(seat_id)
        if record is None or record.guest_id != str(principal.user_id):
            return "no such seat for this account"
        if not record.admitted:
            return f"the seat is {record.status}"
        if sum(1 for r in records.values() if r.admitted) > self.svc.settings.seat_max_guests:
            return "the call has its most guests"
        pool = self.svc.pool
        if pool is not None:
            async with user_tx(pool, principal.user_id) as conn:
                row = await conn.fetchrow(
                    "select session_id, status from app.session_seats where id = $1",
                    UUID(seat_id),
                )
            if row is None or str(row["session_id"]) != sid or row["status"] != "admitted":
                return "the seat is not admitted"
        body: dict[str, Any] = {
            "type": "seat.ready",
            "session_id": sid,
            "seat_id": seat_id,
            "host_name": record.host_name or "the host",
            "mode": state.get("mode") or "interview",
            "capabilities": record.capabilities,
            "host_connected": state.get("status") == "active",
            "sharing": state.get("sharing") != "0",
        }
        if state.get("title"):
            body["title"] = state["title"]
        started = state.get("started_at")
        if started:
            with contextlib.suppress(ValueError):
                body["started_at"] = datetime.fromisoformat(started)
        return record, body

    async def shutdown(self) -> None:
        for g in list(self.guests):
            g.close(CLOSE_GOING_AWAY, "server going away")
