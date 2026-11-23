"""Authenticated same-account replay over existing event streams, never a capture lease."""

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from copilot_core import companions
from copilot_core.auth import AuthError, Principal
from copilot_core.db import user_tx
from copilot_gateway.auth import bearer_token
from copilot_gateway.companionhost import CompanionFold
from copilot_gateway.seatview import FORWARDED

if TYPE_CHECKING:
    from copilot_gateway.session import Services, SessionManager


class CompanionRelay:
    def __init__(self, svc: "Services", manager: "SessionManager") -> None:
        self.svc = svc
        self.manager = manager
        self.sockets: set[WebSocket] = set()

    async def shutdown(self) -> None:
        for ws in tuple(self.sockets):
            with contextlib.suppress(Exception):
                await ws.close(code=1012)

    async def authorize(self, principal: Principal, sid: str, aid: str) -> companions.Attachment:
        state = await self.svc.store.load_state(sid)
        record = await companions.read(self.svc.store.client, sid, aid)
        if not companions.allowed(record, state, str(principal.user_id)):
            raise AuthError("Companion revoked, expired, or no longer belongs to this account")
        assert record is not None
        if await self.manager.as_profile(principal, record.profile_id) is None:
            raise AuthError("Profile access is unavailable")
        if self.svc.pool is None:
            raise AuthError("Attachment cannot be checked")
        async with user_tx(self.svc.pool, principal.user_id) as conn:
            row = await conn.fetchrow(
                "select session_id,profile_id from app.companion_attachments"
                " where id=$1 and revoked_at is null and expires_at>now()",
                UUID(aid),
            )
        if (
            row is None
            or str(row["session_id"]) != sid
            or str(row["profile_id"]) != record.profile_id
        ):
            raise AuthError("Attachment is not active")
        return record

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        receive: asyncio.Task[str] | None = None
        self.sockets.add(ws)

        async def send(body: dict[str, Any]) -> None:
            await asyncio.wait_for(ws.send_json(body), 5)

        try:
            raw = await asyncio.wait_for(
                ws.receive_text(), self.svc.settings.first_message_timeout_s
            )
            if len(raw) > 20000:
                raise AuthError("Join message too large")
            first = companions.Join.model_validate_json(raw)
            token = bearer_token(ws.headers.get("authorization")) or first.access_token or ""
            principal = await self.svc.verifier.verify(token)
            sid, aid = str(first.session_id), str(first.attachment_id)
            record = await self.authorize(principal, sid, aid)
            store = self.svc.store
            _, result_cursor = await store.stream_tail(sid)
            fold = (
                CompanionFold.load(await store.client.get(companions.view_key(sid)))
                or CompanionFold()
            )
            for seq, data in await store.events_after(UUID(sid), fold.seq):
                fold.add(seq, data)
            # A complete reset also handles a trimmed replay window and server migration.
            await self.authorize(principal, sid, aid)
            state = await store.load_state(sid)
            await send(
                {
                    "type": "companion.ready",
                    "session_id": sid,
                    "attachment_id": aid,
                    "audio_source": "desktop",
                    "profile_id": record.profile_id,
                    "spoken_language": state.get("spoken_language") or "en",
                    "language_switching": state.get("language_switching") == "1",
                }
            )
            await send(
                {
                    "type": "companion.snapshot",
                    "seq": fold.seq,
                    "messages": fold.messages(),
                    "reset": True,
                }
            )
            event_cursor = f"{fold.seq}-0"
            receive = asyncio.create_task(ws.receive_text())
            last_command = 0.0
            last_host: bool | None = None
            while True:
                state = await store.load_state(sid)
                if state.get("status") == "ended":
                    await send({"type": "companion.ended", "session_id": sid})
                    return
                # Account/profile/revocation are rechecked before every batch, including
                # private snapshots; a shared-profile owner cannot observe another account.
                record = await self.authorize(principal, sid, aid)
                if principal.expires_at is not None and principal.expires_at <= time.time():
                    raise AuthError("Access token expired")
                host = state.get("status") == "active" and bool(await store.lease_holder(sid))
                if host != last_host:
                    await send(
                        {"type": "companion.host", "connected": host, "audio_source": "desktop"}
                    )
                    last_host = host
                if receive.done():
                    raw = receive.result()
                    receive = asyncio.create_task(ws.receive_text())
                    if len(raw) > 20000:
                        raise AuthError("Message too large")
                    message = json.loads(raw)
                    if message.get("type") == "auth.refresh":
                        renewed = await self.svc.verifier.verify(
                            str(message.get("access_token", ""))
                        )
                        if renewed.user_id != principal.user_id:
                            raise AuthError("Account cannot change on an attachment")
                        principal = renewed
                        await self.authorize(principal, sid, aid)
                    else:
                        try:
                            command = companions.Command.model_validate(message)
                        except ValidationError:
                            await send({"type": "companion.error", "code": "invalid_command"})
                            continue
                        if time.monotonic() - last_command < 0.25:
                            result = "rate_limited"
                        elif not host:
                            result = "denied"
                        else:
                            last_command = time.monotonic()
                            result = await companions.enqueue(store.client, record, command)
                        await send(
                            {
                                "type": "companion.command.result",
                                "command_id": str(command.command_id),
                                "result": result,
                            }
                        )
                events, results = await store.read_feed(sid, event_cursor, result_cursor, 500)
                # End is authoritative, but never emit buffered private content after it.
                if (await store.load_state(sid)).get("status") == "ended":
                    for _cursor, fields in results:
                        if fields.get("s") == aid:
                            await send(
                                {
                                    "type": "companion.command.result",
                                    "command_id": fields.get("c"),
                                    "result": fields.get("r"),
                                }
                            )
                    await send({"type": "companion.ended", "session_id": sid})
                    return
                await self.authorize(principal, sid, aid)
                if events and int(events[0][0].split("-")[0]) > int(event_cursor.split("-")[0]) + 1:
                    # Slow consumers can fall behind the bounded event stream. Reset from
                    # the independently persisted fold instead of showing incomplete chunks.
                    restored = CompanionFold.load(await store.client.get(companions.view_key(sid)))
                    if restored is None:
                        raise AuthError("Replay window unavailable; reconnect")
                    for seq, data in await store.events_after(UUID(sid), restored.seq):
                        restored.add(seq, data)
                    await self.authorize(principal, sid, aid)
                    await send(
                        {
                            "type": "companion.snapshot",
                            "seq": restored.seq,
                            "messages": restored.messages(),
                            "reset": True,
                        }
                    )
                    event_cursor = f"{restored.seq}-0"
                    events = []
                for cursor, data in events:
                    event_cursor = cursor
                    message = json.loads(data)
                    if message.get("type") in FORWARDED:
                        await send(message)
                for cursor, fields in results:
                    result_cursor = cursor
                    if fields.get("s") == aid:
                        await send(
                            {
                                "type": "companion.command.result",
                                "command_id": fields.get("c"),
                                "result": fields.get("r"),
                            }
                        )
        except (AuthError, ValidationError, ValueError):
            with contextlib.suppress(Exception):
                await send({"type": "companion.error", "code": "unauthorized_or_invalid"})
                await ws.close(code=4401)
        except (WebSocketDisconnect, TimeoutError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                await send({"type": "companion.error", "code": "temporarily_unavailable"})
                await ws.close(code=1013)
        finally:
            self.sockets.discard(ws)
            if receive is not None:
                receive.cancel()
                with contextlib.suppress(BaseException):
                    await receive
