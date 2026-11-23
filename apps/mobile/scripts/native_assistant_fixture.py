"""Isolated assistant acceptance fixture; all identities/calls are fictional.

Run with native_fixture.py's base routes and optional local LiveKit dev server.
The API binds only localhost. Use adb reverse for the isolated smoke package.
"""

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from copilot_core.livekit import LiveKit

spec = importlib.util.spec_from_file_location(
    "native_fixture", Path(__file__).with_name("native_fixture.py")
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
SEAT = "88888888-8888-4888-8888-888888888888"


def create_app(media=False):
    app = base.create_app()
    state = {
        "status": "invited",
        "sharing": True,
        "revoked": False,
        "joins": 0,
        "notes": {},
        "media": media,
    }
    app.state.assistant = state
    lk = LiveKit("ws://127.0.0.1:7880", "devkey", "secret")

    def auth(request):
        if request.headers.get("authorization") != f"Bearer {base.TOKEN}":
            raise HTTPException(401)

    def invite():
        return {
            "seat_id": SEAT,
            "session_id": base.SESSION,
            "host_name": "Fictional host",
            "mode": "meeting",
            "title": "Fictional screen review",
            "status": state["status"],
            "capabilities": ["view", "note", "voice", "device.view"],
            "expires_at": None,
        }

    @app.get("/__assistant/host-page")
    async def host_page():
        return FileResponse(Path(__file__).with_name("native_assistant_host.html"))

    @app.get("/__assistant/livekit.js")
    async def sdk():
        return FileResponse(
            Path(__file__).parents[1] / "node_modules/livekit-client/dist/livekit-client.umd.js"
        )

    @app.post("/__assistant/media")
    async def media_state(request: Request):
        state["media_observed"] = await request.json()
        return {"ok": True}

    @app.get("/__assistant")
    async def observed():
        return state

    @app.post("/__assistant")
    async def control(request: Request):
        body = await request.json()
        for key in ("revoked", "sharing", "approve"):
            if isinstance(body.get(key), bool):
                state[key] = body[key]
        return state

    @app.get("/v1/seats")
    async def seats(request: Request):
        auth(request)
        return {"seats": [] if state["revoked"] or state["status"] == "declined" else [invite()]}

    @app.post("/v1/seats/{seat}/{action}")
    async def action(seat: str, action: str, request: Request):
        auth(request)
        if seat != SEAT or state["revoked"]:
            raise HTTPException(404)
        if action == "accept":
            state["status"] = "admitted"
            return {"ok": True}
        if action == "decline":
            state["status"] = "declined"
            return {"ok": True}
        if action not in ("screen", "voice"):
            raise HTTPException(404)
        if state["status"] != "admitted":
            raise HTTPException(403)
        if not media:
            raise HTTPException(503, "Local fixture media server is not enabled.")
        room = f"{action}-{base.SESSION}"
        return {
            "url": lk.url,
            "token": lk.token(
                room,
                f"seat:{SEAT}",
                "Fictional assistant",
                subscribe=action == "screen",
                publish=("microphone",) if action == "voice" else (),
                data=action == "screen",
            ),
            "identity": f"seat:{SEAT}",
            "control": False,
            "can_request_control": True,
        }

    @app.get("/__assistant/host/{kind}")
    async def host_grant(kind: str):
        if kind not in ("screen", "voice"):
            raise HTTPException(404)
        return {
            "url": lk.url,
            "token": lk.token(
                f"{kind}-{base.SESSION}",
                "host",
                "Fictional host",
                subscribe=kind == "voice",
                publish=("screen_share",) if kind == "screen" else (),
                data=kind == "screen",
            ),
        }

    @app.get("/v1/sessions/{sid}/seats")
    async def host_seats(sid: str, request: Request):
        auth(request)
        return {"seats": [], "voice": True, "screen": True}

    @app.websocket("/v1/seat")
    async def socket(ws: WebSocket):
        await ws.accept()
        try:
            if ws.headers.get("authorization") != f"Bearer {base.TOKEN}":
                return await ws.close(4401)
            message = await asyncio.wait_for(ws.receive_json(), 5)
            if (
                message.get("type") != "seat.join"
                or message.get("seat_id") != SEAT
                or message.get("session_id") != base.SESSION
                or state["status"] != "admitted"
                or state["revoked"]
            ):
                return await ws.close(4403)
            state["joins"] += 1
            await ws.send_json(
                {
                    "type": "seat.ready",
                    "seat_id": SEAT,
                    "session_id": base.SESSION,
                    "capabilities": invite()["capabilities"],
                    "sharing": state["sharing"],
                    "host_connected": True,
                }
            )
            await ws.send_json(
                {
                    "type": "seat.snapshot",
                    "seq": 4,
                    "messages": [
                        {
                            "type": "transcript.final",
                            "seq": 1,
                            "seg_id": 1,
                            "channel": "them",
                            "text": "Can you read the fictional release checklist?",
                        },
                        {
                            "type": "question.detected",
                            "seq": 2,
                            "question_id": "q1",
                            "text": "How did the release go?",
                        },
                        {
                            "type": "answer.delta",
                            "seq": 3,
                            "question_id": "q1",
                            "pass": "fast",
                            "text": "The fictional pilot improved the launch.",
                        },
                        {"type": "answer.done", "seq": 4, "question_id": "q1", "pass": "fast"},
                    ],
                }
            )
            prior = state["sharing"]
            while True:
                if state["revoked"]:
                    return await ws.close(4403)
                if prior != state["sharing"]:
                    prior = state["sharing"]
                    await ws.send_json({"type": "seat.host", "connected": True, "sharing": prior})
                try:
                    message = await asyncio.wait_for(ws.receive_json(), 0.25)
                except TimeoutError:
                    continue
                if message.get("type") == "seat.command":
                    key = message.get("command_id")
                    text = message.get("text")
                    result = (
                        "ok"
                        if message.get("kind") == "note"
                        and isinstance(text, str)
                        and 1 <= len(text) <= 1000
                        else "denied"
                    )
                    if result == "ok":
                        state["notes"][key] = text
                    await ws.send_json(
                        {"type": "seat.command.result", "command_id": key, "result": result}
                    )
        except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
            pass

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--media", action="store_true")
    args = parser.parse_args()
    uvicorn.run(create_app(args.media), host="127.0.0.1", port=args.port, access_log=False)
