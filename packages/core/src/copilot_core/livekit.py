"""LiveKit, for guest seats' voice and the host's screen (ARCHITECTURE.md, "Guest seats").

Each live call with seats has up to two LiveKit rooms, so what one grant opens never reaches
the other: `seat-<session id>` for voice, `screen-<session id>` for the host's screen. The API
hands out short access tokens (HS256 JWTs signed with the project's API secret, LiveKit's own
format).

Voice:

- the host's (`host`): subscribe only; the host's app plays what guests say, on a device of
  its choosing, and never publishes anything;
- a guest's (`seat:<seat id>`): publish the microphone only, subscribe to nothing; given only
  for an admitted seat with the `voice` capability, on a call that is live.

A token only gets its holder into the room; it lasts a few minutes. Taking voice away (the host
removes the guest, or turns their voice off) also removes them from the room at once through
LiveKit's server API (`remove`), and the host's app plays no one whose seat lacks `voice`.

The screen:

- the host's (`host`): publish the screen share only (no audio), and data (telling a guest
  when their control starts and ends);
- a guest's: subscribe only, given for an admitted seat with `device.view`; with
  `device.control` too, also send data (their mouse and keys) to the host, whose app carries
  them out only while the host lets that guest control.

Changing a guest's screen grants takes them out of the screen room at once; their app comes
back with a token for what they may do now.
"""

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import jwt

HOST = "host"
TOKEN_TTL_S = 600


def room_of(sid: UUID | str) -> str:
    return f"seat-{sid}"


def screen_room_of(sid: UUID | str) -> str:
    return f"screen-{sid}"


def guest_identity(seat_id: UUID | str) -> str:
    return f"seat:{seat_id}"


class LiveKitError(Exception):
    pass


@dataclass(frozen=True)
class LiveKit:
    url: str  # wss://<project>.livekit.cloud
    api_key: str
    api_secret: str

    def token(
        self, room: str, identity: str, name: str, *, publish: tuple[str, ...] = (),
        subscribe: bool, data: bool = False, ttl_s: int = TOKEN_TTL_S, now: float | None = None,
    ) -> str:  # fmt: skip
        """`publish`: the track sources this participant may send (`microphone`,
        `screen_share`), none by default; `data`: whether it may send data messages."""
        t = int(now if now is not None else time.time())
        video: dict[str, Any] = {
            "room": room,
            "roomJoin": True,
            "canPublish": bool(publish),
            "canSubscribe": subscribe,
            "canPublishData": data,
            "canUpdateOwnMetadata": False,
        }
        if publish:
            video["canPublishSources"] = list(publish)
        claims = {
            "iss": self.api_key, "sub": identity, "name": name,
            "nbf": t - 10, "iat": t, "exp": t + ttl_s, "jti": f"{identity}:{t}",
            "video": video,
        }  # fmt: skip
        return jwt.encode(claims, self.api_secret, algorithm="HS256")

    def _admin(self, room: str) -> str:
        t = int(time.time())
        claims = {
            "iss": self.api_key, "sub": "copilot-api", "nbf": t - 10, "exp": t + 60,
            "video": {"room": room, "roomAdmin": True},
        }  # fmt: skip
        return jwt.encode(claims, self.api_secret, algorithm="HS256")

    @property
    def http_url(self) -> str:
        url = self.url.rstrip("/")
        for ws, http in (("wss://", "https://"), ("ws://", "http://")):
            if url.startswith(ws):
                return http + url[len(ws) :]
        return url

    async def remove(
        self, room: str, identity: str, client: httpx2.AsyncClient | None = None
    ) -> bool:
        """Take one participant out of the room now. False when they were not in it."""
        own = client is None
        client = client or httpx2.AsyncClient(timeout=10)
        try:
            r = await client.post(
                f"{self.http_url}/twirp/livekit.RoomService/RemoveParticipant",
                json={"room": room, "identity": identity},
                headers={"Authorization": f"Bearer {self._admin(room)}"},
            )
        except httpx2.HTTPError as e:
            raise LiveKitError(f"LiveKit could not be reached: {e}") from e
        finally:
            if own:
                await client.aclose()
        if r.status_code == 404:  # not in the room (or no room): nothing to take away
            return False
        if r.status_code >= 400:
            raise LiveKitError(f"LiveKit refused the removal ({r.status_code})")
        return True
