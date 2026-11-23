"""Exercise the fictional native workspace state before using it as UI evidence."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "native_fixture", Path(__file__).parents[1] / "scripts/native_fixture.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
HEADERS = {"Authorization": "Bearer " + fixture.TOKEN, "x-copilot-profile": fixture.PROFILE}


def test_workspace_mutations_are_scoped_and_readable_in_native_fixture():
    with TestClient(fixture.create_app()) as client:
        assert client.get("/v1/employers").status_code == 401
        assert (
            client.get(
                "/v1/employers", headers={**HEADERS, "x-copilot-profile": "other"}
            ).status_code
            == 403
        )
        created = client.post(
            "/v1/employers",
            headers=HEADERS,
            json={"name": "Fictional validation company", "title": "Engineer"},
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        base = f"/v1/employers/{cid}"
        assert client.get(f"{base}/logs/2026-11-23", headers=HEADERS).status_code == 404
        log = client.put(
            f"{base}/logs/2026-11-23", headers=HEADERS, json={"text": "Fictional decision"}
        )
        assert log.json()["text"] == "Fictional decision"
        document = client.post(
            f"{base}/documents",
            headers={**HEADERS, "content-type": "text/markdown", "x-file-name": "notes.md"},
            content=b"# Fictional notes",
        )
        did = document.json()["id"]
        assert client.get(f"{base}/documents/{did}", headers=HEADERS).json()["headings"]
        assert client.delete(f"{base}/documents/{did}", headers=HEADERS).status_code == 204
        assert client.get(f"{base}/documents/{did}", headers=HEADERS).status_code == 404
        assert client.delete(base, headers=HEADERS).status_code == 422
        assert (
            client.delete(
                base, params={"confirm": "Fictional validation company"}, headers=HEADERS
            ).status_code
            == 202
        )


def test_native_application_resume_pairing_and_scheduling():
    with TestClient(fixture.create_app()) as client:
        app = client.post(
            "/v1/applications",
            headers=HEADERS,
            json={"company": "Fictional employer", "role": "Engineer", "jd_text": "Fictional JD"},
        ).json()
        aid = app["id"]
        resume = client.post(
            "/v1/resumes",
            headers=HEADERS,
            json={
                "kind": "tailored",
                "application_id": aid,
                "label": "Fictional role resume",
                "text": "Fictional resume text",
            },
        ).json()
        updated = client.patch(
            f"/v1/applications/{aid}", headers=HEADERS, json={"resume_version_id": resume["id"]}
        ).json()
        assert updated["resume"]["version_id"] == resume["id"]
        interview = client.post(
            "/v1/interviews",
            headers=HEADERS,
            json={"job_id": aid, "round": "technical", "starts_at": "2026-11-24T18:00:00Z"},
        )
        assert interview.status_code == 201
        assert (
            client.get("/v1/interviews", headers=HEADERS).json()["interviews"][0]["job_id"] == aid
        )
        assert client.delete(f"/v1/resumes/{resume['id']}", headers=HEADERS).status_code == 204
        assert client.get(f"/v1/applications/{aid}", headers=HEADERS).json()["resume"] is None
        assert client.delete(f"/v1/applications/{aid}", headers=HEADERS).status_code == 204
        assert client.get("/v1/interviews", headers=HEADERS).json()["interviews"] == []


def test_native_calendar_selection_sync_and_disconnect_clear_preparation():
    app = fixture.create_app()
    with TestClient(app) as client:
        assert client.get("/v1/calendars").status_code == 401
        providers = client.get("/v1/calendars", headers=HEADERS).json()["items"]
        assert providers[0]["connected"] is True
        assert providers[1]["available"] is False
        assert len(client.get("/v1/meeting-preparation", headers=HEADERS).json()["items"]) == 1
        selected = [row["id"] for row in providers[0]["calendars"][:11]]
        assert (
            client.patch(
                "/v1/calendars/google/selection", headers=HEADERS, json={"calendar_ids": selected}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/v1/calendars/google/selection",
                headers=HEADERS,
                json={"calendar_ids": ["outside"]},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/v1/calendars/google/selection",
                headers=HEADERS,
                json={"calendar_ids": ["fictional-calendar"]},
            ).status_code
            == 200
        )
        assert client.get("/v1/meeting-preparation", headers=HEADERS).json()["items"] == []
        app.state.fixture_workspace["calendar_sync_error"] = "Wait before syncing again"
        assert client.post("/v1/calendars/google/sync", headers=HEADERS).status_code == 429
        assert client.get("/v1/calendars", headers=HEADERS).json()["items"][0][
            "selected_calendars"
        ] == ["fictional-calendar"]
        app.state.fixture_workspace["calendar_sync_error"] = None
        assert client.post("/v1/calendars/google/sync", headers=HEADERS).json()["event_count"] == 1
        assert (
            client.delete("/v1/calendars/google", headers=HEADERS).json()["provider_revoked"]
            is True
        )
        preparation = client.get("/v1/meeting-preparation", headers=HEADERS).json()
        assert preparation["items"] == []
        assert preparation["connections"] == []
        assert client.post("/v1/calendars/google/sync", headers=HEADERS).status_code == 409
        # The fixture must never open a real provider with invented OAuth credentials.
        assert client.post("/v1/calendars/google/connect", headers=HEADERS).status_code == 503


def test_native_hubruby_connect_import_refresh_and_disconnect_keep_the_owned_copy():
    app = fixture.create_app()
    with TestClient(app) as client:
        assert client.get("/v1/career-os/connection").status_code == 401
        assert client.get("/v1/career-os/connection", headers=HEADERS).json()["connected"] is False
        assert (
            client.post(
                "/v1/career-os/connect", headers=HEADERS, json={"code": "not-real"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/career-os/connect", headers=HEADERS, json={"code": "FIXTURE-IMPORT"}
            ).status_code
            == 201
        )
        owner = client.get("/v1/profiles", headers=HEADERS).json()["items"][0]
        assert owner["career_os_user_id"] == "fictional-hubruby-user"
        assert (
            client.get("/v1/career-os/applications?stage=offer", headers=HEADERS).json()["items"]
            == []
        )
        assert (
            client.get("/v1/career-os/applications?user_id=other", headers=HEADERS).json()["items"]
            == []
        )
        source = client.get("/v1/career-os/applications?q=HubRuby", headers=HEADERS).json()[
            "items"
        ][0]
        path = f"/v1/career-os/applications/{source['id']}"
        assert client.get(path + "/status", headers=HEADERS).status_code == 404
        pulled = client.post(path + "/pull", headers=HEADERS).json()
        aid = pulled["id"]
        assert pulled["profile_id"] == fixture.PROFILE
        assert pulled["pulled"] == "new"
        assert pulled["resume"]["kind"] == "tailored"
        assert client.get(path + "/status", headers=HEADERS).json()["changed"] is False
        app.state.fixture_workspace["hub"]["revision"] = 2
        assert client.get(path + "/status", headers=HEADERS).json()["changed"] is True
        again = client.post(path + "/pull", headers=HEADERS).json()
        assert again["id"] == aid
        assert again["pulled"] == "again"
        assert "revision 2" in again["jd_text"]
        choices = client.get("/v1/ask/context-options?kind=application", headers=HEADERS).json()[
            "options"
        ]
        assert aid in {item["id"] for item in choices}
        app.state.fixture_workspace["hub"]["only_sign_in_method"] = True
        assert (
            client.delete("/v1/career-os/connection", headers=HEADERS).json()["detail"]["code"]
            == "only_sign_in_method"
        )
        app.state.fixture_workspace["hub"]["only_sign_in_method"] = False
        assert client.delete("/v1/career-os/connection", headers=HEADERS).status_code == 204
        assert (
            client.get("/v1/profiles", headers=HEADERS).json()["items"][0]["career_os_user_id"]
            is None
        )
        assert client.get(f"/v1/applications/{aid}", headers=HEADERS).status_code == 200
        assert client.post(path + "/pull", headers=HEADERS).status_code == 409
