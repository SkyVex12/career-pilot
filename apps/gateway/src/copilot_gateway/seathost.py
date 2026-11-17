"""The host's side of guest seats, inside the host's live session (ARCHITECTURE.md, "Guest
seats").

`SeatDesk` runs as one task of its own beside the session's task group, never inside it: any
failure here is logged and retried, and can never cancel the call. It does three things, and
only on the task that holds the session's lease (it stops with the session):

1. Waits for the session's first seat (a cheap EXISTS once a second, so a call nobody is
   invited to costs one small read per second and no blocking connection).
2. Reads `sess:{sid}:ctl` and carries out each entry: a changed seat list becomes
   `seats.update` for the host; a guest's note is checked against the seat's grant as it
   stands now (admitted, `note`, the same guest, not expired), becomes `seat.note` for the
   host, and its result goes to `sess:{sid}:ctlres` for the relay. A note never touches the
   conversation memory, the turn detector or a prompt, and the session log records that one
   came, never its text. Where the stream was read up to is kept in the session state
   (`ctl_last`), so a resume on another task carries on from there.
3. Keeps the guests' view (`ViewFold`) from every message the host is sent, and writes it to
   `sess:{sid}:view`, fenced by the lease, at most every `seat_view_interval_s` once a seat
   exists.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from copilot_core import seats
from copilot_gateway.seatview import ViewFold
from copilot_gateway.store import UNAVAILABLE
from copilot_protocol import models

if TYPE_CHECKING:
    from copilot_gateway.session import Session

log = logging.getLogger(__name__)

RELIST_S = 5.0  # the seat list is read again this often, so expiries reach the host
PENDING_MAX = 20_000  # messages held while a resumed session restores the guests' view


class SeatDesk:
    def __init__(self, session: "Session", restored: dict[str, str] | None, resumed: bool) -> None:
        self.s = session
        self.store = session.svc.store
        self.settings = session.settings
        restored = restored or {}
        self.ctl_last = restored.get("ctl_last") or "0-0"
        self.fold = ViewFold(sharing=restored.get("sharing", "1") != "0")
        # A session resumed on this task rebuilds the fold from the stored view and the events
        # after it; messages sent meanwhile wait here, so the fold stays in seq order.
        self.restoring = resumed
        self.pending: list[tuple[int, str]] = []
        self.active = False  # a seat exists
        self.last_seats: list[dict[str, Any]] | None = None
        self.view_written_at = 0.0
        self.listed_at = 0.0
        self.force_view = False
        self.done: deque[str] = deque(maxlen=500)  # command ids already answered
        self.notes = 0
        self.task: asyncio.Task[None] | None = None

    # Called by Session.emit for every message the host is sent.
    def observe(self, seq: int, data: str) -> None:
        if not self.settings.seats_enabled:
            return
        if self.restoring:
            # Bounded: a restore that keeps failing (Valkey down) must not hold the whole call.
            if len(self.pending) >= PENDING_MAX:
                del self.pending[: PENDING_MAX // 10]
            self.pending.append((seq, data))
            return
        self.fold.add(seq, data)

    def start(self) -> None:
        if self.settings.seats_enabled and self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _run(self) -> None:
        backoff = 0.2
        while not self.s.ending:
            try:
                await self._loop()
                return
            except asyncio.CancelledError:
                raise
            except UNAVAILABLE as e:
                log.info("seats: valkey unavailable (%s); retrying", type(e).__name__)
            except Exception:
                log.exception("seats: the seat desk failed; retrying (the call goes on)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _loop(self) -> None:
        if self.restoring:
            await self._restore()
        s = self.settings
        while not self.s.ending:
            if not self.active:
                if not await self.store.has_seats(self.s.sid):
                    await asyncio.sleep(s.seat_poll_s)
                    continue
                self.active = True
                await self._seats_changed()
            entries = await self.store.read_ctl(self.s.sid, self.ctl_last, s.seat_block_ms)
            if self.s.ending:
                return
            for entry_id, fields in entries:
                await self._handle(fields)
                self.ctl_last = entry_id
            if entries:
                self.s.writer.set_state(ctl_last=self.ctl_last)
            if time.monotonic() - self.listed_at >= RELIST_S:
                await self._seats_changed()  # an invite that lapsed shows as expired
            await self._write_view()

    async def _restore(self) -> None:
        raw = await self.store.get_view(self.s.sid)
        fold = ViewFold.load(raw) or ViewFold(sharing=self.fold.sharing)
        for seq, data in await self.store.events_after(self.s.sid, fold.seq):
            fold.add(seq, data)
        for seq, data in self.pending:
            fold.add(seq, data)
        self.fold, self.pending, self.restoring = fold, [], False
        self.fold.dirty = True

    async def _handle(self, fields: dict[str, str]) -> None:
        kind = fields.get("k")
        if kind == seats.CTL_SEATS:
            await self._seats_changed()
        elif kind == seats.CTL_NOTE:
            await self._note(fields.get("d") or "{}")

    async def _seats_changed(self) -> None:
        self.listed_at = time.monotonic()
        records = await seats.read_all(self.store.client, self.s.sid)
        listed = [r.info() for r in sorted(records.values(), key=lambda r: r.seat_id)]
        if listed != self.last_seats:
            self.last_seats = listed
            if self.s.knows_seats:
                self.s.emit(models.SeatsUpdate, seats=listed)
            self.s.log_entry(
                {
                    "kind": "seats",
                    "admitted": sum(1 for r in records.values() if r.admitted),
                    "open": sum(1 for r in records.values() if r.effective_status in seats.OPEN),
                }
            )
        if any(r.admitted for r in records.values()):
            self.force_view = True

    async def _note(self, raw: str) -> None:
        try:
            d: dict[str, Any] = json.loads(raw)
            command_id, seat_id = str(d["command_id"]), str(d["seat_id"])
            guest_id, text = str(d["guest_id"]), str(d["text"])
            expires_at = float(d["expires_at"])
        except (ValueError, KeyError, TypeError):
            log.warning("seats: dropped a malformed note")
            return
        if command_id in self.done:
            return
        self.done.append(command_id)
        seat = await seats.read_one(self.store.client, self.s.sid, seat_id)
        if time.time() > expires_at:
            result = "expired"
        elif seat is None or seat.guest_id != guest_id or not seat.may("note"):
            result = "denied"
        elif not self.s.knows_seats or self.s.ending:
            result = "failed"
        else:
            self.s.emit(
                models.SeatNote,
                seat_id=UUID(seat_id),
                note_id=UUID(command_id),
                name=seat.name,
                text=text,
                at=datetime.now(UTC),
            )
            self.notes += 1
            # That a note came, from whom and how long: never its words.
            self.s.log_entry({"kind": "seat.note", "seat_id": seat_id, "chars": len(text)})
            result = "ok"
        await self.store.put_result(self.s.sid, seat_id, command_id, result)

    async def _write_view(self) -> None:
        if not self.active or not (self.fold.dirty or self.force_view):
            return
        now = time.monotonic()
        if not self.force_view and now - self.view_written_at < self.settings.seat_view_interval_s:
            return
        self.fold.dirty, self.force_view = False, False
        self.view_written_at = now
        if not await self.store.put_view(self.s.sid, self.s.svc.task_id, self.fold.dump()):
            log.info("seats: view not written; this task no longer holds the session")
