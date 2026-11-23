"""Local-only fictional backend for native mobile acceptance; never proxies production."""

import argparse
import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from copilot_core import practice as practice_core
from copilot_core.companions import MAX_DISPLAYS, TTL_S
from copilot_core.languages import CALL_LANGUAGES

PROFILE = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
ATTACHMENT = "33333333-3333-4333-8333-333333333333"
TOKEN = "fictional-native-fixture-access"
MODES = {
    "setup_unavailable",
    "normal",
    "slow_attach",
    "reject_attach",
    "missing_gateway",
    "silent_gateway",
    "drop_once",
    "malformed_snapshot",
    "language_rejected",
    "language_ack_first",
    "language_legacy",
}


def create_app():
    app = FastAPI(docs_url=None, redoc_url=None)
    state = {
        "mode": "normal",
        "attached": False,
        "joins": 0,
        "commands": {},
        "seq": 12,
        "attachment_creations": 0,
        "attachments": {},
        "ended": False,
        "events": [],
        "spoken_language": "en",
        "language_results": {},
    }
    app.state.fixture = state

    def auth(request):
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            raise HTTPException(401, "Use the fictional fixture account")
        if request.headers.get("x-copilot-profile") not in (None, PROFILE):
            raise HTTPException(403, "Wrong fixture profile")

    def tokens():
        return {
            "access_token": TOKEN,
            "refresh_token": "fictional-native-fixture-refresh",
            "expires_in": 3600,
            "email": "tester@example.test",
        }

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "fixture": True}

    @app.post("/__fixture")
    async def control(request: Request):
        data = await request.json()
        mode = data.get("mode", "normal")
        if mode not in MODES:
            raise HTTPException(400, "Unknown fixture mode")
        state.update(
            mode=mode,
            joins=0,
            commands={},
            ended=False,
            seq=12,
            events=[],
            spoken_language="en",
            language_results={},
        )
        return {"mode": mode}

    @app.get("/__fixture")
    async def observed():
        return {
            "mode": state["mode"],
            "joins": state["joins"],
            "command_count": len(state["commands"]),
            "attachment_creations": state["attachment_creations"],
            "attached": state["attached"],
            "ended": state["ended"],
            "spoken_language": state["spoken_language"],
        }

    @app.get("/v1/auth/config")
    async def config():
        return {
            "provider": "dev",
            "email": {"code_length": 6, "code_ttl_s": 600, "resend_after_s": 1},
            "career_os": None,
        }

    @app.post("/v1/auth/email/start")
    async def start():
        return {"sent": True}

    @app.post("/v1/auth/email/verify")
    async def verify(request: Request):
        data = await request.json()
        if data.get("code") != "123456" or data.get("email") != "tester@example.test":
            raise HTTPException(401, "Fixture uses tester@example.test and code 123456")
        return tokens()

    @app.post("/v1/auth/token/refresh")
    async def refresh(request: Request):
        if (await request.json()).get("refresh_token") != "fictional-native-fixture-refresh":
            raise HTTPException(401)
        return tokens()

    @app.post("/v1/auth/token/signout")
    async def signout():
        for grant in state["attachments"].values():
            grant["revoked"] = True
        state["attached"] = False
        return {"ok": True}

    @app.get("/v1/profiles")
    async def profiles(request: Request):
        auth(request)
        return {"items": [{"id": PROFILE, "name": "Fictional test profile", "me": True}]}

    @app.get("/v1/client-capabilities")
    async def client_capabilities(request: Request):
        auth(request)
        return {
            "schema_version": 1,
            "server_version": "fictional-native-fixture",
            "contracts": {
                "companion": 1,
                "ask": 1,
                "practice": 1,
                "preparation": 1,
                "languages": 1,
            },
        }

    @app.get("/v1/companion/sessions")
    async def sessions(request: Request):
        auth(request)
        return {
            "items": []
            if state["ended"]
            else [
                {
                    "session_id": SESSION,
                    "profile_id": PROFILE,
                    "title": "Fictional product interview",
                    "mode": "interview",
                    "audio_source": "desktop",
                    "capture_connected": True,
                    "status": "active",
                    "platform": "google_meet",
                    "spoken_language": state["spoken_language"],
                }
            ]
        }

    def active_attachment(grant):
        return (
            grant is not None
            and grant["revoked"] is False
            and datetime.fromisoformat(grant["expires_at"]) > datetime.now(UTC)
        )

    @app.post("/v1/companion/sessions/{session}/attach")
    async def attach(session: str, request: Request):
        auth(request)
        if session != SESSION or state["ended"]:
            raise HTTPException(404)
        body = await request.json()
        device_name = body.get("device_name") if isinstance(body, dict) else None
        if not isinstance(device_name, str) or not 1 <= len(device_name) <= 100:
            raise HTTPException(422, "Use a device name of 1-100 characters")
        if state["mode"] == "slow_attach":
            await asyncio.sleep(25)
        if state["mode"] == "reject_attach":
            raise HTTPException(503, "Fictional attachment failure; retry is safe")
        if sum(active_attachment(grant) for grant in state["attachments"].values()) >= MAX_DISPLAYS:
            raise HTTPException(409, "Disconnect an existing companion before adding another")
        # First ID stays stable for existing native commands; subsequent POSTs are new grants.
        aid = ATTACHMENT if state["attachment_creations"] == 0 else str(uuid4())
        grant = {
            "attachment_id": aid,
            "session_id": SESSION,
            "profile_id": PROFILE,
            "device_name": device_name,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=TTL_S)).isoformat(),
            "revoked": False,
        }
        state["attachments"][aid] = grant
        state["attachment_creations"] += 1
        state["attached"] = True
        return {
            **grant,
            "account_id": PROFILE,
            "expires_at": datetime.fromisoformat(grant["expires_at"]).timestamp(),
            "audio_source": "desktop",
            "spoken_language": state["spoken_language"],
        }

    @app.get("/v1/companion/attachments")
    async def attachments(request: Request):
        auth(request)
        return {"items": list(reversed(list(state["attachments"].values())))[:100]}

    @app.delete("/v1/companion/attachments/{attachment}")
    async def detach(attachment: str, request: Request):
        auth(request)
        grant = state["attachments"].get(attachment)
        if grant is None:
            raise HTTPException(404)
        grant["revoked"] = True
        state["attached"] = any(active_attachment(item) for item in state["attachments"].values())
        return {"ok": True}

    @app.get("/v1/profile/call-preferences")
    async def call_preferences(request: Request):
        auth(request)
        return {"spoken_language": "en"}

    @app.get("/v1/call-languages")
    async def languages(request: Request):
        auth(request)
        if state["mode"] == "setup_unavailable":
            raise HTTPException(503, "Fictional setup outage")
        return {
            "items": [
                {
                    "id": code,
                    "name": language.name,
                    "native_name": language.native_name,
                    "direction": language.direction,
                }
                for code, language in CALL_LANGUAGES.items()
            ],
            "fixture": True,
            "capability_notice": (
                "Synthetic selector catalogue only; no speech or model provider was exercised."
            ),
        }

    # These routes simulate application behavior, never AI/provider behavior.
    application_id = "44444444-4444-4444-8444-444444444444"
    history_id = "55555555-5555-4555-8555-555555555555"
    connection_id = "66666666-6666-4666-8666-666666666666"
    stamp = datetime.now(UTC).isoformat()
    saved_call = {
        "id": history_id,
        "title": "Fictional launch retrospective",
        "mode": "interview",
        "status": "ready",
        "started_at": stamp,
        "outputs": {"summary": "Fictional fixture: a small pilot improved a product launch."},
        "notes": "Fictional notes: send the pilot checklist.",
    }
    segments = [
        {
            "seg_id": 1,
            "channel": "them",
            "text": "Tell me about a fictional product launch.",
            "start_ms": 12000,
        },
        {
            "seg_id": 2,
            "channel": "me",
            "text": "I gathered pilot feedback and adjusted the release checklist.",
            "start_ms": 18000,
        },
    ]
    application = {
        "application_id": application_id,
        "profile_id": PROFILE,
        "company": "Fictional Acme",
        "job_title": "Product engineer",
    }
    conversations, practices, practice_keys, polls = {}, {}, {}, {}
    contexts = {
        "calendar_id": "fictional-calendar",
        "instance_id": "fictional-meeting",
        "application_id": application_id,
        "session_id": history_id,
        "unfinished_actions": ["Send the fictional pilot checklist"],
    }
    history_deleted = False
    source_id = f"{history_id}:them:1"

    def source_detail():
        if history_deleted:
            raise HTTPException(404, "Fictional source deleted")
        return {
            "kind": "transcript",
            "id": source_id,
            "label": "Fictional launch retrospective",
            "content": segments[0]["text"],
            "excerpt": segments[0]["text"],
            "date": stamp,
            "start_ms": 12000,
            "citation": "S1",
        }

    @app.get("/v1/sessions")
    async def history(request: Request):
        auth(request)
        return {"sessions": [] if history_deleted else [saved_call], "next_before": None}

    @app.get("/v1/sessions/{sid}")
    async def history_detail(sid: str, request: Request):
        auth(request)
        if sid != history_id or history_deleted:
            raise HTTPException(404)
        return saved_call

    @app.get("/v1/sessions/{sid}/transcript")
    async def transcript(sid: str, request: Request):
        await history_detail(sid, request)
        return {"stored": True, "segments": segments}

    @app.delete("/v1/sessions/{sid}")
    async def delete_call(sid: str, request: Request):
        nonlocal history_deleted
        await history_detail(sid, request)
        history_deleted = True
        return {"deleted": True}

    @app.get("/v1/ask/context-options")
    async def options(request: Request, kind: str = "application"):
        auth(request)
        choices = {
            "application": [{"id": application_id, "label": "Fictional Acme · Product engineer"}],
            "call": [] if history_deleted else [{"id": history_id, "label": saved_call["title"]}],
            "company": [],
            "document": [],
        }
        return {"options": choices.get(kind, [])}

    @app.get("/v1/ask/conversations")
    async def ask_list(request: Request):
        auth(request)
        return {"conversations": list(conversations.values())}

    @app.post("/v1/ask/conversations", status_code=201)
    async def ask_create(request: Request):
        auth(request)
        body = await request.json()
        cid = str(uuid4())
        conversation = {
            "id": cid,
            "title": body.get("title") or "Fictional Ask conversation",
            "scope": body.get("scope", {"kind": "auto"}),
            "messages": [],
            "retrieval_notice": "Fictional local fixture; no model or private provider is called.",
            "retention_notice": "Fixture data exists only in this test process.",
        }
        conversations[cid] = conversation
        return conversation

    def conversation(cid):
        if cid not in conversations:
            raise HTTPException(404)
        return conversations[cid]

    @app.get("/v1/ask/conversations/{cid}")
    async def ask_get(cid: str, request: Request):
        auth(request)
        value = conversation(cid)
        for message in value["messages"]:
            if message["status"] == "pending":
                polls[message["id"]] = polls.get(message["id"], 0) + 1
                if polls[message["id"]] >= 2:
                    message["status"] = "done"
                    message["answer"] = (
                        "Fictional fixture answer: describe your pilot, feedback, and next step."
                        + (
                            " [S1]"
                            if value["scope"]["kind"] != "general" and not history_deleted
                            else ""
                        )
                    )
                    message["sources"] = (
                        [source_detail()]
                        if value["scope"]["kind"] != "general" and not history_deleted
                        else []
                    )
        return value

    @app.patch("/v1/ask/conversations/{cid}")
    async def ask_rename(cid: str, request: Request):
        auth(request)
        value = conversation(cid)
        body = await request.json()
        value["title"] = str(body.get("title", value["title"]))
        return value

    @app.delete("/v1/ask/conversations/{cid}")
    async def ask_delete(cid: str, request: Request):
        auth(request)
        conversation(cid)
        del conversations[cid]
        return {"deleted": True}

    @app.post("/v1/ask/conversations/{cid}/messages", status_code=202)
    async def ask_send(cid: str, request: Request):
        auth(request)
        value = conversation(cid)
        body = await request.json()
        for message in value["messages"]:
            if message.get("request_id") == body.get("request_id"):
                if message["question"] != body.get("question"):
                    raise HTTPException(409)
                return message
        if not body.get("question", "").strip():
            raise HTTPException(422)
        if body.get("scope"):
            value["scope"] = body["scope"]
        message = {
            "id": str(uuid4()),
            "request_id": body.get("request_id"),
            "question": body["question"],
            "answer": "",
            "status": "pending",
            "sources": [],
            "error": None,
        }
        value["messages"].append(message)
        return message

    @app.post("/v1/ask/conversations/{cid}/messages/{mid}/{action}")
    async def ask_action(cid: str, mid: str, action: str, request: Request):
        auth(request)
        value = conversation(cid)
        message = next((m for m in value["messages"] if m["id"] == mid), None)
        if message is None:
            raise HTTPException(404)
        if action not in ("retry", "cancel"):
            raise HTTPException(404)
        message["status"] = "pending" if action == "retry" else "cancelled"
        polls[mid] = 0
        return message

    @app.get("/v1/ask/sources/{kind}/{sid}")
    async def ask_source(kind: str, sid: str, request: Request):
        auth(request)
        if kind != "transcript" or sid != source_id:
            raise HTTPException(404)
        return source_detail()

    @app.get("/v1/practice/context")
    async def practice_context(request: Request):
        auth(request)
        return {
            "sources": [
                {
                    "kind": "application",
                    "id": application_id,
                    "label": "Fictional Acme application",
                    "excerpt": "Fictional product role: explain delivery and feedback.",
                }
            ]
        }

    def practice_view(sid):
        if sid not in practices:
            raise HTTPException(404)
        return {
            "id": sid,
            **{k: v for k, v in practices[sid].items() if k != "pending"},
            "voice_available": False,
            "speech_available": False,
            "retention_notice": "Fictional text simulation; no speech or model provider.",
        }

    @app.get("/v1/practice/sessions")
    async def practice_list(request: Request):
        auth(request)
        return {"sessions": [practice_view(sid) for sid in practices]}

    @app.post("/v1/practice/sessions", status_code=201)
    async def practice_create(request: Request):
        auth(request)
        body = await request.json()
        key = body.get("request_id")
        if key in practice_keys:
            if practice_keys[key]["body"] != body:
                raise HTTPException(409)
            return practice_view(practice_keys[key]["id"])
        setup = practice_core.Setup.model_validate(body)
        imported = None
        if setup.source_question:
            selected = setup.source_question
            if str(selected.session_id) != history_id or history_deleted:
                raise HTTPException(404)
            segment = next(
                (
                    s
                    for s in segments
                    if s["seg_id"] == selected.seg_id and s["channel"] == selected.channel
                ),
                None,
            )
            if segment is None:
                raise HTTPException(404)
            imported = {"kind": "transcript", "id": source_id, "content": segment["text"]}
        sid = str(uuid4())
        practices[sid] = practice_core.new_state(setup, [], setup.spoken_language or "en", imported)
        practice_keys[key] = {"id": sid, "body": copy.deepcopy(body)}
        polls[sid] = 0
        return practice_view(sid)

    @app.get("/v1/practice/sessions/{sid}")
    async def practice_get(sid: str, request: Request):
        auth(request)
        practice_view(sid)
        value = practices[sid]
        if value["status"] in ("preparing", "processing"):
            polls[sid] = polls.get(sid, 0) + 1
            if polls[sid] >= 2:
                pending = value["pending"]
                if pending["kind"] == "plan":
                    result = {
                        "plan": ["Fictional pilot", "Learning"],
                        "question": "Describe a fictional pilot and its outcome.",
                        "question_kind": "hypothetical",
                    }
                elif pending["kind"] in ("hint", "sample"):
                    result = {"text": "Fictional coaching: explain your action and its result."}
                else:
                    answer = (
                        practice_core.current(value, pending.get("question_id"))["attempts"][-1][
                            "answer"
                        ]
                        if pending["kind"] == "turn"
                        else ""
                    )
                    result = {
                        "question": (
                            f"Fictional round {len(value['questions']) + 1}: what would you change?"
                        ),
                        "question_kind": "hypothetical",
                        "feedback": {
                            "dimensions": {
                                "relevance": 3,
                                "structure": 2,
                                "evidence": 2,
                                "reasoning": 3,
                                "tradeoffs": 2,
                            },
                            "strengths": ["Fictional fixture: a clear action was described."],
                            "improvements": ["Add an observable outcome."],
                            "excerpts": [answer] if answer else [],
                            "uncertainty": [
                                "Fixed fixture feedback; no model assessment occurred."
                            ],
                            "suggested_wording": "I tried [action] and learned [lesson].",
                        },
                    }
                practice_core.apply_result(value, result)
        return practice_view(sid)

    @app.post("/v1/practice/sessions/{sid}/actions")
    async def practice_action(sid: str, request: Request):
        auth(request)
        practice_view(sid)
        body = await request.json()
        key = (sid, body.get("request_id"))
        if key in practice_keys:
            if practice_keys[key] != body:
                raise HTTPException(409)
            return practice_view(sid)
        try:
            practice_core.reduce(practices[sid], practice_core.Action.model_validate(body))
        except practice_core.PracticeError as exc:
            raise HTTPException(409, str(exc)) from exc
        practice_keys[key] = copy.deepcopy(body)
        polls[sid] = 0
        return practice_view(sid)

    @app.delete("/v1/practice/sessions/{sid}")
    async def practice_delete(sid: str, request: Request):
        auth(request)
        practice_view(sid)
        del practices[sid]
        return {"deleted": True}

    @app.get("/v1/meeting-preparation")
    async def preparation(request: Request):
        auth(request)
        return {
            "items": [
                {
                    "meeting": {
                        "account_id": connection_id,
                        "calendar_id": contexts["calendar_id"],
                        "instance_id": contexts["instance_id"],
                        "title": "Fictional Acme interview",
                        "start": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                        "end": (datetime.now(UTC) + timedelta(minutes=40)).isoformat(),
                    },
                    "candidates": [application],
                    "selected_application_id": contexts["application_id"],
                    "linked_session_id": None if history_deleted else contexts["session_id"],
                    "unfinished_actions": contexts["unfinished_actions"],
                    "requires_confirmation": False,
                    "overlaps": False,
                    "previous": [
                        {
                            "notes": "" if history_deleted else saved_call["notes"],
                            "unfinished_actions": [],
                        }
                    ],
                }
            ],
            "connections": [
                {
                    "connection_id": connection_id,
                    "provider": "google",
                    "status": "active",
                    "last_sync_at": stamp,
                    "last_error": None,
                }
            ],
            "applications": [application],
            "saved_sessions": []
            if history_deleted
            else [{"id": history_id, "title": saved_call["title"], "started_at": stamp}],
        }

    @app.put("/v1/calendars/{provider}/context")
    async def preparation_save(provider: str, request: Request):
        auth(request)
        body = await request.json()
        if (
            provider != "google"
            or body.get("calendar_id") != contexts["calendar_id"]
            or body.get("instance_id") != contexts["instance_id"]
        ):
            raise HTTPException(404)
        if body.get("application_id") not in (None, application_id) or body.get(
            "session_id"
        ) not in (None, history_id):
            raise HTTPException(404)
        if body.get("session_id") and history_deleted:
            raise HTTPException(404)
        contexts.update(body)
        return {"saved": True}

    @app.websocket("/v1/companion")
    async def companion(ws: WebSocket):
        if state["mode"] == "missing_gateway":
            await ws.close()
            return
        await ws.accept()
        try:
            join = await asyncio.wait_for(ws.receive_json(), 10)
            grant = state["attachments"].get(join.get("attachment_id"))
            if (
                join.get("access_token") != TOKEN
                or join.get("session_id") != SESSION
                or not active_attachment(grant)
            ):
                await ws.close(4403)
                return
            state["joins"] += 1
            if state["mode"] == "silent_gateway":
                await asyncio.sleep(30)
                return
            await ws.send_json(
                {
                    "type": "companion.ready",
                    "session_id": SESSION,
                    "spoken_language": state["spoken_language"],
                    "language_switching": state["mode"] != "language_legacy",
                }
            )
            if state["mode"] == "malformed_snapshot":
                await ws.send_json(
                    {
                        "type": "companion.snapshot",
                        "messages": [
                            {
                                "type": "question.detected",
                                "question_id": "bad",
                                "text": {"invalid": True},
                            }
                        ],
                    }
                )
            else:
                # Actual fold insertion order: q2 precedes the latest merged q1 delta in sequence.
                await ws.send_json(
                    {
                        "type": "companion.snapshot",
                        "reset": True,
                        "seq": state["seq"],
                        "messages": [
                            {
                                "type": "question.detected",
                                "seq": 1,
                                "question_id": "q1",
                                "text": "Tell me about a fictional launch.",
                            },
                            {
                                "type": "answer.delta",
                                "seq": 11,
                                "question_id": "q1",
                                "pass": "fast",
                                "text": "I organized a small pilot and measured adoption.",
                            },
                            {
                                "type": "question.detected",
                                "seq": 5,
                                "question_id": "q2",
                                "text": "What did you learn?",
                            },
                            {
                                "type": "answer.delta",
                                "seq": 6,
                                "question_id": "q2",
                                "pass": "fast",
                                "text": "Early feedback improved the fictional release.",
                            },
                            {"type": "answer.done", "seq": 7, "question_id": "q2", "pass": "fast"},
                            {"type": "answer.done", "seq": 12, "question_id": "q1", "pass": "fast"},
                        ]
                        + state["events"],
                    }
                )
            await ws.send_json(
                {"type": "companion.host", "connected": True, "audio_source": "desktop"}
            )
            if state["mode"] == "drop_once" and state["joins"] == 1:
                await asyncio.sleep(1)
                await ws.close(1012)
                return
            while True:
                message = await ws.receive_json()
                if message.get("type") == "auth.refresh":
                    continue
                if message.get("type") != "companion.command":
                    continue
                key = message.get("command_id")
                if not key:
                    continue
                if key in state["commands"]:
                    await ws.send_json(state["commands"][key])
                    if key in state["language_results"]:
                        await ws.send_json(state["language_results"][key])
                    continue
                if message.get("kind") == "set_language":
                    requested = message.get("spoken_language")
                    rejected = (
                        state["mode"] == "language_rejected" or requested not in CALL_LANGUAGES
                    )
                    if not rejected:
                        state["spoken_language"] = requested
                    state["seq"] += 1
                    acknowledgement = {
                        "type": "session.language",
                        "seq": state["seq"],
                        "request_id": key,
                        "spoken_language": state["spoken_language"],
                    }
                    if rejected:
                        acknowledgement["error"] = (
                            "Fictional language change rejected. Try another language."
                        )
                    response = {
                        "type": "companion.command.result",
                        "command_id": key,
                        "result": "executed",
                    }
                    state["commands"][key] = response
                    state["language_results"][key] = acknowledgement
                    state["events"].append(acknowledgement)
                    frames = (
                        [acknowledgement, response]
                        if state["mode"] == "language_ack_first"
                        else [response, acknowledgement]
                    )
                    for frame in frames:
                        await ws.send_json(frame)
                    continue
                result = "executed"
                if message.get("kind") == "end" and not message.get("confirmed"):
                    result = "denied"
                response = {"type": "companion.command.result", "command_id": key, "result": result}
                state["commands"][key] = response
                await ws.send_json(response)
                if result == "denied":
                    continue
                if message.get("kind") == "end":
                    state["ended"] = True
                    await ws.send_json({"type": "companion.ended"})
                    return
                state["seq"] += 1
                qid = "generated-" + str(state["seq"])
                detected = {
                    "type": "question.detected",
                    "seq": state["seq"],
                    "question_id": qid,
                    "text": message.get("text") or "Fictional next question",
                }
                state["events"].append(detected)
                await ws.send_json(detected)
                for kind, extra in [
                    (
                        "answer.delta",
                        {"pass": "fast", "text": "A fictional live answer arrived successfully."},
                    ),
                    ("answer.done", {}),
                ]:
                    state["seq"] += 1
                    output = {"type": kind, "seq": state["seq"], "question_id": qid, **extra}
                    state["events"].append(output)
                    await ws.send_json(output)
        except (TimeoutError, WebSocketDisconnect):
            pass

    return app


app = create_app()
if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Deliberately no host flag: use adb reverse, never a LAN/public listener.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
