"""Guest seats (ARCHITECTURE.md, "Guest seats"): invite by email, accept, admit, change,
remove, and the limits, against the docker compose stack. A live call is the gateway's state
in Valkey, written here as the gateway writes it."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import jwt as pyjwt
import pytest
from pydantic import SecretStr

from copilot_api.testing import Api
from copilot_core import keys, seats

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]


async def live_call(api: Api, uid: uuid.UUID, status: str = "active") -> uuid.UUID:
    sid = uuid.uuid4()
    await api.svc.valkey.hset(
        keys.sess_state(sid),
        mapping={"user_id": str(uid), "status": status, "mode": "interview", "title": "Acme"},
    )
    return sid


async def account(api: Api, name: str) -> tuple[uuid.UUID, dict[str, str], str]:
    uid, h = api.user(name)
    r = await api.client.get("/v1/me", headers=h)  # the first request makes the account
    assert r.status_code == 200, r.text
    return uid, h, r.json()["email"]


async def mirror(api: Api, sid: uuid.UUID, seat_id: str) -> seats.SeatRecord | None:
    return await seats.read_one(api.svc.valkey, sid, seat_id)


async def invite(api: Api, h: dict[str, str], sid: uuid.UUID, email: str, **body: Any) -> Any:
    return await api.client.post(
        f"/v1/sessions/{sid}/seats", headers=h, json={"email": email, **body}
    )


async def test_an_invite_lets_the_guest_in_at_once(api: Api) -> None:
    """The host's invite is the permission: the seat is admitted, and the guest can join with
    nothing to accept and no one to wait for. The host can still narrow or remove it."""
    host, hh, _ = await account(api, "Alex")
    guest, gh, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)

    r = await invite(api, hh, sid, f"  {guest_email.upper()} ")
    assert (r.status_code, r.json()) == (202, {"status": "sent"})
    listed = (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()["seats"]
    assert [(s["name"], s["status"], s["capabilities"]) for s in listed] == [
        ("Blair", "admitted", ["view", "note"])
    ]
    assert listed[0]["admitted_at"] is not None
    seat_id = listed[0]["seat_id"]
    rec = await mirror(api, sid, seat_id)
    assert rec is not None
    assert rec.admitted
    assert rec.host_name == "Alex"
    assert rec.guest_id == str(guest)
    ctl = await api.svc.valkey.xrange(keys.sess_ctl(sid))
    assert ctl
    assert ctl[-1][1] == {b"k": b"seats"}

    mine = (await api.client.get("/v1/seats", headers=gh)).json()["seats"]
    assert [(s["seat_id"], s["status"], s["host_name"], s["title"]) for s in mine] == [
        (seat_id, "admitted", "Alex", "Acme")
    ]
    # Narrower: notes off. The mirror carries it at once, with a new rev.
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat_id}", headers=hh, json={"capabilities": []}
    )
    assert r.json()["capabilities"] == ["view"]
    rec = await mirror(api, sid, seat_id)
    assert rec is not None
    assert rec.capabilities == ["view"]
    assert rec.rev == 2
    r = await api.client.delete(f"/v1/sessions/{sid}/seats/{seat_id}", headers=hh)
    assert r.status_code == 204
    assert (await mirror(api, sid, seat_id)).status == "removed"  # type: ignore[union-attr]
    assert (await api.client.get("/v1/seats", headers=gh)).json()["seats"] == []
    # Removing again is a retry: it writes the mirror again. An unknown seat is 404.
    r = await api.client.delete(f"/v1/sessions/{sid}/seats/{seat_id}", headers=hh)
    assert r.status_code == 204
    r = await api.client.delete(f"/v1/sessions/{sid}/seats/{uuid.uuid4()}", headers=hh)
    assert r.status_code == 404
    # Removed, the guest may be invited again.
    assert (await invite(api, hh, sid, guest_email)).status_code == 202


async def test_an_invite_says_nothing_about_who_has_an_account(api: Api) -> None:
    host, hh, host_email = await account(api, "Alex")
    sid = await live_call(api, host)
    for email in ("nobody-here@example.com", host_email):
        r = await invite(api, hh, sid, email)
        assert (r.status_code, r.json()) == (202, {"status": "sent"})
    assert (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()["seats"] == []
    assert await api.svc.valkey.exists(keys.sess_seats(sid)) == 0


async def test_only_the_host_of_a_live_call_invites(api: Api) -> None:
    host, hh, _ = await account(api, "Alex")
    _, oh, _ = await account(api, "Other")
    _, _, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)
    assert (await invite(api, oh, sid, guest_email)).status_code == 404
    ended = await live_call(api, host, status="ended")
    assert (await invite(api, hh, ended, guest_email)).status_code == 409
    assert (await invite(api, hh, sid, guest_email, capabilities=["voice"])).status_code == 422
    assert (await invite(api, hh, uuid.uuid4(), guest_email)).status_code == 404


async def test_a_guest_sees_only_their_own_seats_and_only_while_live(api: Api) -> None:
    host, hh, _ = await account(api, "Alex")
    _, gh, guest_email = await account(api, "Blair")
    _, oh, _ = await account(api, "Other")
    sid = await live_call(api, host)
    await invite(api, hh, sid, guest_email)
    assert len((await api.client.get("/v1/seats", headers=gh)).json()["seats"]) == 1
    assert (await api.client.get("/v1/seats", headers=oh)).json()["seats"] == []
    await api.svc.valkey.hset(keys.sess_state(sid), "status", "ended")
    assert (await api.client.get("/v1/seats", headers=gh)).json()["seats"] == []
    # After the call the guest has nothing of it, as the host's History does.
    assert (await api.client.get(f"/v1/sessions/{sid}", headers=gh)).status_code == 404


async def test_limits(api: Api) -> None:
    host, hh, _ = await account(api, "Alex")
    sid = await live_call(api, host)
    guests = [await account(api, f"Guest {i}") for i in range(seats.MAX_GUESTS + 1)]
    # Every invite admits: at most MAX_GUESTS of them per call.
    for _, _, email in guests[: seats.MAX_GUESTS]:
        assert (await invite(api, hh, sid, email)).status_code == 202
    assert (await invite(api, hh, sid, guests[-1][2])).status_code == 409
    # Invites per hour, per host account, across calls.
    other = await live_call(api, host)
    sent = seats.MAX_GUESTS + 1
    codes = []
    for _ in range(seats.INVITES_PER_HOUR - sent + 1):
        codes.append((await invite(api, hh, other, "nobody-here@example.com")).status_code)
    assert codes[-1] == 429
    assert set(codes[:-1]) == {202}


async def test_the_mirror_round_trips(api: Api) -> None:
    record = seats.SeatRecord("s", "g", "Blair", "admitted", ["view", "note"], 3, "Alex", 1.0)
    again = seats.SeatRecord.from_json(record.to_json())
    assert again == record
    assert again.effective_status == "expired"
    assert not again.may("note")  # expired in 1970
    assert json.loads(record.to_json())["host_name"] == "Alex"


async def test_host_changes_can_be_sent_again(api: Api) -> None:
    """A change recorded while Valkey could not take it is sent again: the mirror follows."""
    host, hh, _ = await account(api, "Alex")
    _, gh, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)
    await invite(api, hh, sid, guest_email)
    seat_id = (await api.client.get("/v1/seats", headers=gh)).json()["seats"][0]["seat_id"]
    path = f"/v1/sessions/{sid}/seats/{seat_id}"
    await api.svc.valkey.delete(keys.sess_seats(sid))  # as if the mirror write was lost
    assert (await api.client.patch(path, headers=hh, json={"admit": True})).status_code == 200
    rec = await mirror(api, sid, seat_id)
    assert rec is not None
    assert rec.admitted
    assert (await api.client.delete(path, headers=hh)).status_code == 204
    await api.svc.valkey.delete(keys.sess_seats(sid))
    assert (await api.client.delete(path, headers=hh)).status_code == 204
    rec = await mirror(api, sid, seat_id)
    assert rec is not None
    assert rec.status == "removed"


@pytest.fixture
def voice_on(api: Api) -> Iterator[None]:
    """A LiveKit project for the API (a name that resolves nowhere: removals log and go on)."""
    s = api.svc.settings
    s.livekit_url, s.livekit_api_key = "wss://lk.invalid", "APItest"
    s.livekit_api_secret = SecretStr("t" * 40)
    try:
        yield
    finally:
        s.livekit_url = s.livekit_api_key = s.livekit_api_secret = None


def lk_claims(token: str) -> dict[str, Any]:
    return pyjwt.decode(token, "t" * 40, algorithms=["HS256"])


async def test_voice_is_granted_only_where_livekit_is_set_up(api: Api) -> None:
    host, hh, _ = await account(api, "Alex")
    _, _, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)
    r = await invite(api, hh, sid, guest_email, capabilities=["view", "voice"])
    assert r.status_code == 422
    listed = (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()
    assert listed["voice"] is False  # the app offers no voice switch
    assert (await api.client.post(f"/v1/sessions/{sid}/voice", headers=hh)).status_code == 404


async def test_the_host_listens_and_a_guest_with_voice_speaks(api: Api, voice_on: None) -> None:
    host, hh, _ = await account(api, "Alex")
    _, gh, guest_email = await account(api, "Blair")
    _, oh, other_email = await account(api, "Other")
    sid = await live_call(api, host)
    assert (
        await invite(api, hh, sid, guest_email, capabilities=["view", "voice"])
    ).status_code == 202
    assert (await invite(api, hh, sid, other_email)).status_code == 202  # no voice
    mine = {
        s["name"]: s
        for s in (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()["seats"]
    }

    assert (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()["voice"] is True
    r = await api.client.post(f"/v1/sessions/{sid}/voice", headers=hh)
    assert r.status_code == 200, r.text
    host_token = r.json()
    assert (host_token["url"], host_token["room"], host_token["identity"]) == (
        "wss://lk.invalid",
        f"seat-{sid}",
        "host",
    )
    video = lk_claims(host_token["token"])["video"]
    assert (video["canSubscribe"], video["canPublish"]) == (True, False)

    seat = mine["Blair"]["seat_id"]
    r = await api.client.post(f"/v1/seats/{seat}/voice", headers=gh)
    assert r.status_code == 200, r.text
    c = lk_claims(r.json()["token"])
    assert c["sub"] == f"seat:{seat}"
    assert (c["video"]["canPublish"], c["video"]["canSubscribe"]) == (True, False)
    assert c["video"]["canPublishSources"] == ["microphone"]

    # No voice granted, someone else's seat, or voice taken away: no token.
    other = mine["Other"]["seat_id"]
    assert (await api.client.post(f"/v1/seats/{other}/voice", headers=oh)).status_code == 403
    assert (await api.client.post(f"/v1/seats/{seat}/voice", headers=oh)).status_code == 404
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat}", headers=hh, json={"capabilities": ["view"]}
    )
    assert r.status_code == 200  # LiveKit unreachable here: the removal is logged, not fatal
    assert (await api.client.post(f"/v1/seats/{seat}/voice", headers=gh)).status_code == 403
    await api.svc.valkey.hset(keys.sess_state(sid), "status", "ended")
    assert (await api.client.post(f"/v1/sessions/{sid}/voice", headers=hh)).status_code == 409


async def test_the_screen_is_granted_only_where_livekit_is_set_up(api: Api) -> None:
    host, hh, _ = await account(api, "Alex")
    _, _, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)
    r = await invite(api, hh, sid, guest_email, capabilities=["view", "device.view"])
    assert r.status_code == 422
    listed = (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()
    assert listed["screen"] is False
    assert (await api.client.post(f"/v1/sessions/{sid}/screen", headers=hh)).status_code == 404


async def test_control_needs_view(api: Api, voice_on: None) -> None:
    host, hh, _ = await account(api, "Alex")
    _, _, guest_email = await account(api, "Blair")
    sid = await live_call(api, host)
    r = await invite(api, hh, sid, guest_email, capabilities=["view", "device.control"])
    assert r.status_code == 422


async def test_the_host_shares_the_screen_and_a_guest_watches(
    api: Api, voice_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[uuid.UUID] = []

    async def drop_screen(_svc: object, _session: uuid.UUID, seat: uuid.UUID) -> None:
        dropped.append(seat)

    monkeypatch.setattr("copilot_api.routes.seats._drop_screen", drop_screen)
    host, hh, _ = await account(api, "Alex")
    _, gh, guest_email = await account(api, "Blair")
    _, oh, other_email = await account(api, "Other")
    sid = await live_call(api, host)
    r = await invite(api, hh, sid, guest_email, capabilities=["view", "device.view"])
    assert r.status_code == 202
    assert (await invite(api, hh, sid, other_email)).status_code == 202  # no screen
    listed = (await api.client.get(f"/v1/sessions/{sid}/seats", headers=hh)).json()
    assert listed["screen"] is True
    mine = {s["name"]: s for s in listed["seats"]}

    # The host: its own room, sharing the screen only.
    r = await api.client.post(f"/v1/sessions/{sid}/screen", headers=hh)
    assert r.status_code == 200, r.text
    assert (r.json()["room"], r.json()["identity"]) == (f"screen-{sid}", "host")
    video = lk_claims(r.json()["token"])["video"]
    assert video["canPublishSources"] == ["screen_share"]
    assert video["canPublishData"] is True

    # A viewer may send quality and consent requests, but has no input grant.
    seat = mine["Blair"]["seat_id"]
    r = await api.client.post(f"/v1/seats/{seat}/screen", headers=gh)
    assert r.status_code == 200, r.text
    assert r.json()["control"] is False
    assert r.json()["can_request_control"] is True
    video = lk_claims(r.json()["token"])["video"]
    assert (video["canPublish"], video["canSubscribe"], video["canPublishData"]) == (
        False,
        True,
        True,
    )

    # With control: data too, still nothing published.
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat}",
        headers=hh,
        json={"capabilities": ["view", "device.view", "device.control"]},
    )
    assert r.status_code == 200, r.text
    r = await api.client.post(f"/v1/seats/{seat}/screen", headers=gh)
    assert r.json()["control"] is True
    assert dropped == []  # Approval must preserve the viewer and pending consent request.
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat}",
        headers=hh,
        json={"capabilities": ["view", "device.view"]},
    )
    assert r.status_code == 200
    assert dropped == []  # Revocation is enforced by the host input gate without a room bounce.
    r = await api.client.post(f"/v1/seats/{seat}/screen", headers=gh)
    assert r.json()["control"] is False
    video = lk_claims(r.json()["token"])["video"]
    assert (video["canPublish"], video["canPublishData"]) == (False, True)

    # Without the grant, someone else's seat, the grant taken away, or the call over: none.
    other = mine["Other"]["seat_id"]
    assert (await api.client.post(f"/v1/seats/{other}/screen", headers=oh)).status_code == 403
    assert (await api.client.post(f"/v1/seats/{seat}/screen", headers=oh)).status_code == 404
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat}", headers=hh, json={"capabilities": ["view"]}
    )
    assert r.status_code == 200
    assert (await api.client.post(f"/v1/seats/{seat}/screen", headers=gh)).status_code == 403
    # An approval based on stale viewer permissions cannot restore revoked screen access.
    r = await api.client.patch(
        f"/v1/sessions/{sid}/seats/{seat}",
        headers=hh,
        json={
            "capabilities": ["view", "device.view", "device.control"],
            "expected_capabilities": ["view", "device.view"],
        },
    )
    assert r.status_code == 409
    assert "permissions changed" in r.json()["detail"]
    assert (await api.client.post(f"/v1/seats/{seat}/screen", headers=gh)).status_code == 403
    assert dropped == [uuid.UUID(seat)]
    await api.svc.valkey.hset(keys.sess_state(sid), "status", "ended")
    assert (await api.client.post(f"/v1/sessions/{sid}/screen", headers=hh)).status_code == 409


async def test_mobile_host_voice_does_not_replace_desktop_and_is_receive_only(
    api: Api, voice_on: None
) -> None:
    host, hh, _ = await account(api, "Mobile host")
    _, other, _ = await account(api, "Unrelated")
    sid = await live_call(api, host)
    device_id = uuid.uuid4()
    desktop = await api.client.post(f"/v1/sessions/{sid}/voice", headers=hh)
    mobile = await api.client.post(f"/v1/sessions/{sid}/voice?device_id={device_id}", headers=hh)
    assert desktop.status_code == mobile.status_code == 200
    assert desktop.json()["identity"] == "host"
    assert mobile.json()["identity"] == f"host:{device_id}"
    video = lk_claims(mobile.json()["token"])["video"]
    assert video["canSubscribe"] is True
    assert video["canPublish"] is False
    assert video["canPublishData"] is False
    assert (
        await api.client.post(f"/v1/sessions/{sid}/voice?device_id={device_id}", headers=other)
    ).status_code == 404
    assert (
        await api.client.post(f"/v1/sessions/{sid}/voice?device_id=invalid", headers=hh)
    ).status_code == 422
