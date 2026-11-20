"""Validate the native acceptance fixture itself before interpreting device results."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "native_fixture", Path(__file__).parents[1] / "scripts/native_fixture.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
HEADERS = {"Authorization": "Bearer " + fixture.TOKEN, "x-copilot-profile": fixture.PROFILE}


def attach(client):
    return client.post(
        f"/v1/companion/sessions/{fixture.SESSION}/attach",
        headers=HEADERS,
        json={"device_name": "Test device"},
    )


def join(ws, attachment_id=fixture.ATTACHMENT):
    ws.send_json(
        {
            "type": "companion.join",
            "session_id": fixture.SESSION,
            "attachment_id": attachment_id,
            "access_token": fixture.TOKEN,
            "last_seq": 0,
        }
    )
    assert ws.receive_json()["type"] == "companion.ready"
    snapshot = ws.receive_json()
    assert ws.receive_json()["type"] == "companion.host"
    return snapshot


def test_auth_and_profile_scope():
    with TestClient(fixture.create_app()) as c:
        assert c.get("/healthz").json()["fixture"]
        assert c.get("/v1/auth/config").json()["email"]["code_length"] == 6
        assert (
            c.post(
                "/v1/auth/email/verify", json={"email": "tester@example.test", "code": "000000"}
            ).status_code
            == 401
        )
        token = c.post(
            "/v1/auth/email/verify", json={"email": "tester@example.test", "code": "123456"}
        ).json()["access_token"]
        assert token == fixture.TOKEN
        assert c.get("/v1/profiles").status_code == 401
        assert c.get("/v1/profiles", headers=HEADERS).json()["items"][0]["id"] == fixture.PROFILE
        assert (
            c.get(
                "/v1/companion/sessions", headers={**HEADERS, "x-copilot-profile": "other"}
            ).status_code
            == 403
        )


def test_snapshot_live_commands_and_idempotent_replay():
    with TestClient(fixture.create_app()) as c:
        assert attach(c).status_code == 200
        with c.websocket_connect("/v1/companion") as ws:
            snapshot = join(ws)
            assert [m["seq"] for m in snapshot["messages"]] == [1, 11, 5, 6, 7, 12]
            assert snapshot["seq"] == 12
            assert {
                m["question_id"] for m in snapshot["messages"] if m["type"] == "answer.done"
            } == {"q1", "q2"}
            command = {
                "type": "companion.command",
                "command_id": "fixture-command",
                "kind": "followup",
                "text": "Fictional followup",
            }
            ws.send_json(command)
            assert ws.receive_json()["result"] == "executed"
            assert ws.receive_json()["text"] == "Fictional followup"
            assert "live answer" in ws.receive_json()["text"]
            assert ws.receive_json()["type"] == "answer.done"
            ws.send_json(command)
            assert ws.receive_json()["result"] == "executed"
            assert c.get("/__fixture").json()["command_count"] == 1
            ws.send_json(
                {"type": "companion.command", "command_id": "end", "kind": "end", "confirmed": True}
            )
            assert ws.receive_json()["result"] == "executed"
            assert ws.receive_json()["type"] == "companion.ended"
        assert c.get("/v1/companion/sessions", headers=HEADERS).json()["items"] == []


def test_attach_error_and_malformed_snapshot_are_controllable():
    with TestClient(fixture.create_app()) as c:
        c.post("/__fixture", json={"mode": "reject_attach"})
        assert attach(c).status_code == 503
        c.post("/__fixture", json={"mode": "malformed_snapshot"})
        assert attach(c).status_code == 200
        with c.websocket_connect("/v1/companion") as ws:
            assert isinstance(join(ws)["messages"][0]["text"], dict)
        assert c.post("/__fixture", json={"mode": "unknown"}).status_code == 400


def test_reconnect_replays_generated_answer_without_execution_again():
    with TestClient(fixture.create_app()) as c:
        attach(c)
        with c.websocket_connect("/v1/companion") as ws:
            join(ws)
            ws.send_json({"type": "companion.command", "command_id": "replay", "kind": "answer"})
            for _ in range(4):
                ws.receive_json()
        with c.websocket_connect("/v1/companion") as ws:
            snapshot = join(ws)
            assert snapshot["seq"] == 15
            assert any(
                m.get("text") == "A fictional live answer arrived successfully."
                for m in snapshot["messages"]
            )
            assert c.get("/__fixture").json()["command_count"] == 1


def test_ask_pending_done_source_and_deleted_source():
    with TestClient(fixture.create_app()) as c:
        conv = c.post(
            "/v1/ask/conversations", headers=HEADERS, json={"scope": {"kind": "auto"}}
        ).json()
        url = "/v1/ask/conversations/" + conv["id"]
        body = {"request_id": "fixture-message", "question": "What did the pilot teach me?"}
        pending = c.post(url + "/messages", headers=HEADERS, json=body).json()
        assert pending["status"] == "pending"
        assert c.post(url + "/messages", headers=HEADERS, json=body).json()["id"] == pending["id"]
        assert c.get(url, headers=HEADERS).json()["messages"][0]["status"] == "pending"
        done = c.get(url, headers=HEADERS).json()["messages"][0]
        assert done["status"] == "done"
        citation = done["sources"][0]
        source_path = f"/v1/ask/sources/{citation['kind']}/{citation['id']}"
        assert "fictional product launch" in c.get(source_path, headers=HEADERS).json()["content"]
        history = c.get("/v1/sessions", headers=HEADERS).json()["sessions"][0]
        assert c.delete("/v1/sessions/" + history["id"], headers=HEADERS).status_code == 200
        assert c.get(source_path, headers=HEADERS).status_code == 404


def test_saved_transcript_import_practice_feedback_and_finish():
    from uuid import uuid4

    with TestClient(fixture.create_app()) as c:
        history = c.get("/v1/sessions", headers=HEADERS).json()["sessions"][0]
        segment = c.get(f"/v1/sessions/{history['id']}/transcript", headers=HEADERS).json()[
            "segments"
        ][0]
        assert c.get("/v1/practice/context", headers=HEADERS).json()["sources"]
        body = {
            "request_id": str(uuid4()),
            "topic": "Fictional pilot",
            "duration_minutes": 15,
            "spoken_language": "en",
            "source_question": {
                "session_id": history["id"],
                "channel": segment["channel"],
                "seg_id": segment["seg_id"],
            },
        }
        created = c.post("/v1/practice/sessions", headers=HEADERS, json=body)
        assert created.status_code == 201, created.text
        value = created.json()
        path = "/v1/practice/sessions/" + value["id"]
        assert value["status"] == "preparing"
        assert not value["voice_available"]
        assert (
            c.post("/v1/practice/sessions", headers=HEADERS, json=body).json()["id"] == value["id"]
        )
        for _ in range(2):
            value = c.get(path, headers=HEADERS).json()
        assert value["status"] == "answering"
        assert value["questions"][0]["text"] == segment["text"]
        answer = {
            "request_id": str(uuid4()),
            "action": "answer",
            "question_id": value["current_question_id"],
            "answer": "I collected feedback and changed the checklist.",
        }
        assert (
            c.post(path + "/actions", headers=HEADERS, json=answer).json()["status"] == "processing"
        )
        c.post(path + "/actions", headers=HEADERS, json=answer)
        for _ in range(2):
            value = c.get(path, headers=HEADERS).json()
        attempts = value["questions"][0]["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["feedback"]["rubric_version"]
        assert attempts[0]["feedback"]["excerpts"] == [answer["answer"]]
        assert "fixture" in attempts[0]["feedback"]["uncertainty"][0]
        finished = c.post(
            path + "/actions",
            headers=HEADERS,
            json={"request_id": str(uuid4()), "action": "finish"},
        ).json()
        assert finished["status"] == "finished"


def test_calendar_correction_persists_all_selections_and_cleared_actions():
    with TestClient(fixture.create_app()) as c:
        prep = c.get("/v1/meeting-preparation", headers=HEADERS).json()
        item = prep["items"][0]
        assert prep["applications"]
        assert prep["saved_sessions"]
        body = {
            "calendar_id": item["meeting"]["calendar_id"],
            "instance_id": item["meeting"]["instance_id"],
            "application_id": None,
            "session_id": None,
            "unfinished_actions": [],
        }
        assert c.put("/v1/calendars/google/context", headers=HEADERS, json=body).json() == {
            "saved": True
        }
        changed = c.get("/v1/meeting-preparation", headers=HEADERS).json()["items"][0]
        assert changed["selected_application_id"] is None
        assert changed["linked_session_id"] is None
        assert changed["unfinished_actions"] == []


def test_process_recreation_can_reuse_complete_grant_without_another_attachment_post():
    device_name = "Android phone · 77777777-7777-4777-8777-777777777777"
    with TestClient(fixture.create_app()) as c:
        path = f"/v1/companion/sessions/{fixture.SESSION}/attach"
        created = c.post(path, headers=HEADERS, json={"device_name": device_name})
        assert created.status_code == 200
        grant = created.json()
        listed = c.get("/v1/companion/attachments", headers=HEADERS).json()["items"]
        assert len(listed) == 1
        assert listed[0]["attachment_id"] == grant["attachment_id"]
        assert set(listed[0]) == {
            "attachment_id",
            "session_id",
            "profile_id",
            "device_name",
            "expires_at",
            "revoked",
        }
        assert listed[0]["device_name"] == device_name
        assert isinstance(listed[0]["expires_at"], str)
        assert isinstance(grant["expires_at"], float)
        assert grant["account_id"] == fixture.PROFILE
        assert grant["device_name"] == device_name
        assert grant["profile_id"] == fixture.PROFILE
        assert grant["session_id"] == fixture.SESSION
        assert grant["revoked"] is False
        with c.websocket_connect("/v1/companion") as ws:
            join(ws, grant["attachment_id"])
        # A lost process/socket does not revoke its grant. Read and rejoin exactly that grant.
        existing = c.get("/v1/companion/attachments", headers=HEADERS).json()["items"][0]
        with c.websocket_connect("/v1/companion") as ws:
            join(ws, existing["attachment_id"])
        observed = c.get("/__fixture").json()
        assert observed["attachment_creations"] == 1
        assert observed["joins"] == 2
        assert (
            c.delete(
                "/v1/companion/attachments/" + grant["attachment_id"], headers=HEADERS
            ).status_code
            == 200
        )
        assert (
            c.get("/v1/companion/attachments", headers=HEADERS).json()["items"][0]["revoked"]
            is True
        )
        assert c.get("/__fixture").json()["attached"] is False


def test_attachment_creations_are_distinct_bounded_and_errors_do_not_count():
    with TestClient(fixture.create_app()) as c:
        path = f"/v1/companion/sessions/{fixture.SESSION}/attach"
        for body in ({}, {"device_name": 1}, {"device_name": ""}, {"device_name": "x" * 101}):
            assert c.post(path, headers=HEADERS, json=body).status_code == 422
        c.post("/__fixture", json={"mode": "reject_attach"})
        assert attach(c).status_code == 503
        assert c.get("/__fixture").json()["attachment_creations"] == 0
        c.post("/__fixture", json={"mode": "normal"})
        grants = []
        for index in range(fixture.MAX_DISPLAYS):
            response = c.post(
                path, headers=HEADERS, json={"device_name": f"Fictional device {index}"}
            )
            assert response.status_code == 200
            grants.append(response.json())
        assert len({grant["attachment_id"] for grant in grants}) == fixture.MAX_DISPLAYS
        assert c.get("/__fixture").json()["attachment_creations"] == fixture.MAX_DISPLAYS
        assert attach(c).status_code == 409
        assert c.get("/__fixture").json()["attachment_creations"] == fixture.MAX_DISPLAYS
        c.delete("/v1/companion/attachments/" + grants[0]["attachment_id"], headers=HEADERS)
        assert attach(c).status_code == 200
        assert c.get("/__fixture").json()["attachment_creations"] == fixture.MAX_DISPLAYS + 1
        listed = c.get("/v1/companion/attachments", headers=HEADERS).json()["items"]
        assert len(listed) == fixture.MAX_DISPLAYS + 1
        assert sum(not grant["revoked"] for grant in listed) == fixture.MAX_DISPLAYS


def test_fixture_language_selector_uses_all_actual_catalog_labels_but_claims_no_speech_support():
    with TestClient(fixture.create_app()) as c:
        assert c.get("/v1/call-languages").status_code == 401
        response = c.get("/v1/call-languages", headers=HEADERS).json()
        expected = [
            {
                "id": code,
                "name": language.name,
                "native_name": language.native_name,
                "direction": language.direction,
            }
            for code, language in fixture.CALL_LANGUAGES.items()
        ]
        assert len(response["items"]) == 24
        assert response["items"] == expected
        assert response["fixture"] is True
        assert "Synthetic selector" in response["capability_notice"]
        assert "no speech or model provider" in response["capability_notice"]
