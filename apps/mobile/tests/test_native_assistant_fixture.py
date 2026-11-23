"""Contract checks for the local, fictional native assistant acceptance server."""

import importlib.util
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "native_assistant_fixture", Path(__file__).parents[1] / "scripts/native_assistant_fixture.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
HEADERS = {"Authorization": "Bearer " + fixture.base.TOKEN}


def test_assistant_fixture_invitation_scope_notes_and_revocation():
    with TestClient(fixture.create_app()) as client:
        assert client.get("/v1/seats").status_code == 401
        seat = client.get("/v1/seats", headers=HEADERS).json()["seats"][0]
        assert seat["status"] == "invited"
        assert client.post(f"/v1/seats/{fixture.SEAT}/accept", headers=HEADERS).status_code == 200
        with client.websocket_connect("/v1/seat", headers=HEADERS) as ws:
            ws.send_json(
                {"type": "seat.join", "seat_id": fixture.SEAT, "session_id": fixture.base.SESSION}
            )
            assert ws.receive_json()["type"] == "seat.ready"
            assert len(ws.receive_json()["messages"]) == 4
            command = {
                "type": "seat.command",
                "command_id": "one",
                "kind": "note",
                "text": "Fictional private note",
            }
            ws.send_json(command)
            assert ws.receive_json()["result"] == "ok"
            ws.send_json(command)
            assert ws.receive_json()["result"] == "ok"
            assert len(client.get("/__assistant").json()["notes"]) == 1
            client.post("/__assistant", json={"sharing": False})
            assert ws.receive_json() == {"type": "seat.host", "connected": True, "sharing": False}
            client.post("/__assistant", json={"revoked": True})
            assert ws.receive()["code"] == 4403
        assert client.get("/__fixture").json()["ended"] is False


def test_assistant_fixture_media_grants_match_real_publish_boundaries():
    with TestClient(fixture.create_app(media=True)) as client:
        client.post(f"/v1/seats/{fixture.SEAT}/accept", headers=HEADERS)
        for kind in ("screen", "voice"):
            grant = client.post(f"/v1/seats/{fixture.SEAT}/{kind}", headers=HEADERS).json()
            claims = jwt.decode(grant["token"], "secret", algorithms=["HS256"])
            assert claims["sub"] == f"seat:{fixture.SEAT}"
            assert claims["video"]["canPublish"] is (kind == "voice")
            assert claims["video"]["canSubscribe"] is (kind == "screen")
            assert claims["video"]["canPublishData"] is (kind == "screen")
            if kind == "voice":
                assert claims["video"]["canPublishSources"] == ["microphone"]
