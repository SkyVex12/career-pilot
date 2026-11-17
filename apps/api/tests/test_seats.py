"""Guest seats (ARCHITECTURE.md, "Guest seats"): invite by email, accept, admit, change,
remove, and the limits, against the docker compose stack. A live call is the gateway's state
in Valkey, written here as the gateway writes it."""

import json
import uuid
from typing import Any

import pytest

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
