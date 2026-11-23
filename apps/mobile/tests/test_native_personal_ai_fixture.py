"""Confirm the fictional native Personal AI journey follows the real REST schemas."""

import copy
import importlib.util
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from copilot_core import personal_ai as context

spec = importlib.util.spec_from_file_location(
    "native_fixture", Path(__file__).parents[1] / "scripts/native_fixture.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
HEADERS = {"Authorization": "Bearer " + fixture.TOKEN, "x-copilot-profile": fixture.PROFILE}
BASE = "/v1/personal-ai/requests"


def seed(client):
    return client.get(BASE, headers=HEADERS).json()["requests"][0]


def test_seeded_brief_and_scoped_create_have_actual_request_contracts():
    with TestClient(fixture.create_app()) as client:
        assert client.get(BASE).status_code == 401
        assert (
            client.get(BASE, headers={**HEADERS, "x-copilot-profile": "other"}).status_code == 403
        )
        ready = seed(client)
        assert ready["id"] == ready["request_id"]
        assert ready["profile_id"] == fixture.PROFILE
        assert ready["active_version_id"] == ready["active_brief"]["id"]
        assert ready["active_brief"]["request_id"] == ready["id"]
        assert ready["active_brief"]["origin"] == "paste"
        assert ready["expected_brief_format"] == context.Brief.model_json_schema()
        context.Preparation.model_validate(ready["scope"])
        brief = context.Brief.model_validate(ready["active_brief"]["brief"])
        assert len(brief.items) == 2
        assert ready["active_brief"]["content"] == context.brief_content(brief)

        scope = {**ready["scope"], "title": "Fictional scoped request"}
        scope["employer_id"] = str(uuid4())
        assert client.post(BASE, headers=HEADERS, json=scope).status_code == 404
        companies = client.get("/v1/employers", headers=HEADERS).json()
        scope["employer_id"] = companies["employers"][0]["id"]
        created = client.post(BASE, headers=HEADERS, json=scope)
        assert created.status_code == 201, created.text
        value = created.json()
        assert value["scope"] == scope
        assert value["request_revision"] == 1
        assert value["active_version_id"] is None
        assert "active_brief" not in value
        assert client.get(BASE + "/" + value["id"], headers=HEADERS).json() == value


def test_publish_correction_exclusion_revision_and_delete_follow_receipts():
    app = fixture.create_app()
    with TestClient(app) as client:
        ready = seed(client)
        created = client.post(BASE, headers=HEADERS, json=ready["scope"]).json()
        path = BASE + "/" + created["id"]
        body = {
            "request_revision": 1,
            "idempotency_key": "fictional-import",
            "brief": ready["active_brief"]["brief"],
        }
        receipt = client.post(path + "/publish", headers=HEADERS, json=body).json()
        assert receipt["request_id"] == created["id"]
        assert receipt["duplicate"] is False
        repeated = client.post(path + "/publish", headers=HEADERS, json=body).json()
        assert repeated == {**receipt, "duplicate": True}
        changed = copy.deepcopy(body)
        changed["brief"]["title"] = "Different fictional content"
        assert client.post(path + "/publish", headers=HEADERS, json=changed).status_code == 409

        corrected = client.patch(
            path + "/items/pilot_decision",
            headers=HEADERS,
            json={"expected_version_id": receipt["version_id"], "text": "A fictional correction."},
        ).json()
        assert corrected["version_id"] != receipt["version_id"]
        assert corrected["request_id"] == created["id"]
        assert (
            client.patch(
                path + "/items/pilot_next",
                headers=HEADERS,
                json={"expected_version_id": receipt["version_id"], "text": None},
            ).status_code
            == 409
        )
        excluded = client.patch(
            path + "/items/pilot_next",
            headers=HEADERS,
            json={"expected_version_id": corrected["version_id"], "text": None},
        ).json()
        value = client.get(path, headers=HEADERS).json()
        assert value["active_brief"]["origin"] == "correction"
        assert value["active_version_id"] == excluded["version_id"]
        [fact] = value["active_brief"]["brief"]["items"]
        assert fact["text"] == "A fictional correction."
        assert fact["provenance"] == "user_report"
        assert fact["source_ids"] == []

        revision = client.post(
            path + "/refresh", headers=HEADERS, json={"expected_revision": 1}
        ).json()
        assert revision["request_revision"] == 2
        assert revision["active_version_id"] == excluded["version_id"]
        assert "active_brief" not in revision  # Actual refresh route returns only request data.
        assert (
            client.post(
                path + "/refresh", headers=HEADERS, json={"expected_revision": 1}
            ).status_code
            == 409
        )
        assert client.post(path + "/publish", headers=HEADERS, json=body).status_code == 409
        body.update(request_revision=2, idempotency_key="fictional-refresh")
        assert client.post(path + "/publish", headers=HEADERS, json=body).status_code == 200
        refreshed = client.get(path, headers=HEADERS).json()
        assert refreshed["active_brief"]["brief"]["items"] == [fact]
        assert client.delete(path, headers=HEADERS).status_code == 204
        assert client.get(path, headers=HEADERS).status_code == 404
        assert client.post(path + "/publish", headers=HEADERS, json=body).status_code == 404
        assert all(
            value["request_id"] != created["id"]
            for value in app.state.fixture_personal_ai["versions"].values()
        )


def test_invalid_briefs_are_rejected_and_client_revocation_preserves_saved_brief():
    with TestClient(fixture.create_app()) as client:
        ready = seed(client)
        invalid = copy.deepcopy(ready["active_brief"]["brief"])
        invalid["items"][0]["source_ids"] = ["missing_source"]
        path = BASE + "/" + ready["id"]
        assert (
            client.post(
                path + "/publish",
                headers=HEADERS,
                json={"request_revision": 1, "idempotency_key": "invalid", "brief": invalid},
            ).status_code
            == 422
        )
        connections = "/v1/personal-ai/oauth/connections"
        assert client.get(connections).status_code == 401
        [connection] = client.get(connections, headers=HEADERS).json()["connections"]
        assert set(connection) == {"id", "client_id", "scope", "expires_at"}
        assert isinstance(connection["expires_at"], int)
        assert connection["scope"] == "context:read context:publish"
        revoke = connections + "/" + connection["id"]
        assert client.delete(revoke, headers=HEADERS).json() == {}
        assert client.delete(revoke, headers=HEADERS).status_code == 200
        assert client.get(connections, headers=HEADERS).json() == {"connections": []}
        assert client.get(path, headers=HEADERS).json() == ready
