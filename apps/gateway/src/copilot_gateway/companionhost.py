"""Same-account display snapshots and controls, independent of the capture transport."""

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from copilot_core import companions, keys
from copilot_gateway.seatview import FORWARDED, ViewFold
from copilot_protocol import models

if TYPE_CHECKING:
    from copilot_gateway.session import Session

log = logging.getLogger(__name__)
WRITE_VIEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""


class CompanionFold(ViewFold):
    def add_message(self, message: dict[str, Any]) -> None:
        self.seq = max(self.seq, int(message.get("seq", 0)))
        if message.get("type") in FORWARDED:
            self._put(message)
            self.dirty = True


class CompanionDesk:
    def __init__(self, session: "Session", restored: dict[str, str]) -> None:
        self.s = session
        self.store = session.svc.store
        self.fold = CompanionFold()
        self.restoring = bool(restored)
        self.pending: list[tuple[int, str]] = []
        self.cursor = restored.get("companion_ctl_last", "0-0")
        self.task: asyncio.Task[None] | None = None

    def observe(self, seq: int, data: str) -> None:
        if self.restoring:
            self.pending.append((seq, data))
            self.pending = self.pending[-20000:]
        else:
            self.fold.add(seq, data)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run(self) -> None:
        while not self.s.ending:
            try:
                if self.restoring:
                    self.fold = (
                        CompanionFold.load(
                            await self.store.client.get(companions.view_key(self.s.sid))
                        )
                        or CompanionFold()
                    )
                    for seq, data in await self.store.events_after(self.s.sid, self.fold.seq):
                        self.fold.add(seq, data)
                    for seq, data in self.pending:
                        self.fold.add(seq, data)
                    self.pending.clear()
                    self.restoring = False
                entries = await self.store.read_ctl(self.s.sid, self.cursor, 1000)
                for cursor, fields in entries:
                    if fields.get("k") == companions.CTL:
                        await self.command(fields.get("d", "{}"))
                    self.cursor = cursor
                if entries:
                    self.s.writer.set_state(companion_ctl_last=self.cursor)
                if self.fold.dirty:
                    await self.store.client.eval(
                        WRITE_VIEW,
                        2,
                        keys.sess_lease(self.s.sid),
                        companions.view_key(self.s.sid),
                        self.s.svc.task_id,
                        self.fold.dump(),
                        companions.TTL_S,
                    )
                    self.fold.dirty = False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Companion desk retrying; capture continues")
                await asyncio.sleep(1)

    async def command(self, raw: str) -> None:
        try:
            body = json.loads(raw)
            command = companions.Command.model_validate(body["command"])
            aid, account = str(body["attachment_id"]), str(body["account_id"])
            expires = float(body["expires_at"])
        except (ValueError, TypeError, KeyError):
            return
        cid, sid = str(command.command_id), str(self.s.sid)
        if not await companions.claim(self.store.client, sid, cid):
            return
        state = await self.store.load_state(sid)
        record = await companions.read(self.store.client, sid, aid)
        if expires < time.time():
            result = "expired"
        elif (
            self.s.ending
            or str(self.s.account_id) != account
            or self.s.private_context_account_id not in (None, "", account)
            or not companions.allowed(record, state, account)
            or await self.store.lease_holder(sid) != self.s.svc.task_id
        ):
            result = "denied"
        else:
            before = self.s.seq
            if command.kind == "end":
                await self.s.handle(
                    models.SessionEnd(type="session.end", summarize=command.summarize)
                )
            elif command.kind == "set_sharing":
                assert command.sharing is not None
                await self.s.handle(models.SharingSet(type="sharing.set", on=command.sharing))
            elif command.kind == "set_language":
                assert command.spoken_language is not None
                await self.s.handle(
                    models.SessionSetLanguage(
                        type="session.set_language",
                        spoken_language=command.spoken_language,
                        request_id=command.command_id,
                    )
                )
            else:
                await self.s.handle(models.QuestionForce(type="question.force", text=command.text))
            result = "sharing_unavailable" if command.kind == "set_sharing" else "executed"
            for seq, data in self.s.ring:
                if seq > before:
                    message = json.loads(data)
                    if (
                        command.kind == "set_sharing"
                        and message.get("type") == "sharing"
                        and message.get("on") is command.sharing
                    ):
                        result = "executed"
                    if message.get("code") in {"rate_limited", "nothing_to_answer"}:
                        result = message["code"]
                    if (
                        message.get("type") == "session.language"
                        and message.get("request_id") == cid
                        and message.get("error")
                    ):
                        result = "language_unavailable"
        await companions.finish(self.store.client, sid, cid, result)
        await self.store.put_result(self.s.sid, aid, cid, result)
