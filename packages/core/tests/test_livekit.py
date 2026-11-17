"""LiveKit tokens and removal for guest seats' voice (copilot_core.livekit): the host only
listens, a guest only sends their microphone, and a removal takes a guest out at once."""

import json

import httpx2
import jwt
import pytest

from copilot_core.livekit import HOST, LiveKit, LiveKitError, guest_identity, room_of

LK = LiveKit("wss://copilot-test.livekit.cloud", "APIkey123", "s" * 40)
SID = "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b33"
SEAT = "0192f3d0-0000-7a00-8000-0000000000b1"


def claims(token: str) -> dict[str, object]:
    return jwt.decode(
        token,
        LK.api_secret,
        algorithms=["HS256"],
        options={"verify_nbf": False, "verify_exp": False},
    )


def test_the_host_listens_and_never_publishes() -> None:
    c = claims(LK.token(room_of(SID), HOST, "Host", publish_mic=False, subscribe=True, now=1000))
    assert (c["iss"], c["sub"], c["exp"]) == ("APIkey123", "host", 1600)
    assert c["video"] == {
        "room": f"seat-{SID}", "roomJoin": True, "canPublish": False, "canSubscribe": True,
        "canPublishData": False, "canUpdateOwnMetadata": False,
    }  # fmt: skip


def test_a_guest_sends_only_their_microphone_and_hears_nothing() -> None:
    token = LK.token(room_of(SID), guest_identity(SEAT), "Blair", publish_mic=True, subscribe=False)
    video = claims(token)["video"]
    assert isinstance(video, dict)
    assert video["canPublish"] is True
    assert video["canPublishSources"] == ["microphone"]
    assert video["canSubscribe"] is False
    assert video["canPublishData"] is False


async def test_a_removal_asks_livekit_to_take_the_guest_out() -> None:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        assert await LK.remove(room_of(SID), guest_identity(SEAT), client) is True
    (req,) = seen
    assert str(req.url) == (
        "https://copilot-test.livekit.cloud/twirp/livekit.RoomService/RemoveParticipant"
    )
    assert json.loads(req.content) == {"room": f"seat-{SID}", "identity": f"seat:{SEAT}"}
    admin = claims(req.headers["Authorization"].removeprefix("Bearer "))
    assert admin["video"] == {"room": f"seat-{SID}", "roomAdmin": True}


async def test_a_guest_not_in_the_room_is_no_error_and_a_refusal_is() -> None:
    for status, expected in ((404, False), (500, None)):
        transport = httpx2.MockTransport(lambda _r, status=status: httpx2.Response(status))
        async with httpx2.AsyncClient(transport=transport) as client:
            if expected is None:
                with pytest.raises(LiveKitError):
                    await LK.remove("r", "i", client)
            else:
                assert await LK.remove("r", "i", client) is expected
