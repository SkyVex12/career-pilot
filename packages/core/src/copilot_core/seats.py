"""Guest seats on a live call: the shared vocabulary of the API (which records seats in
`app.session_seats` and mirrors each one into Valkey), the host's gateway session (which
carries out guests' commands) and the seat relay (which serves guests). ARCHITECTURE.md,
"Guest seats".

The Valkey mirror `sess:{sid}:seats` maps each seat id to a `SeatRecord` as JSON. Postgres is
the record; the mirror is what the gateway checks on every command and every second of a
guest's connection, so a removal or a narrower grant takes effect at once on any gateway task.
`rev` goes up with every change, so a check can tell which grant it saw.

After each change the API also appends `{"k": "seats"}` to `sess:{sid}:ctl`, so the host's
session sends the host the new list.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from copilot_core import keys

# Every capability the schema knows, and the ones this release grants. A capability outside
# GRANTABLE is refused by the API, and never honoured by the gateway.
CAPABILITIES = (
    "view", "note", "voice", "control.answer", "control.session", "device.view", "device.control",
)  # fmt: skip
GRANTABLE = frozenset({"view", "note", "voice", "device.view", "device.control"})
# The capabilities carried over LiveKit: granted only where it is set up.
MEDIA = frozenset({"voice", "device.view", "device.control"})

OPEN = ("invited", "accepted", "admitted")
CLOSED = ("declined", "removed", "expired", "ended")

# Limits (ARCHITECTURE.md, "Guest seats"): admitted guests per call, invites still open per
# call, invites a host may send per hour, how long an invite waits for an answer and an
# admission, and how long any seat lasts at most (a call ends long before).
MAX_GUESTS = 2
MAX_OPEN = 5
INVITES_PER_HOUR = 10
INVITE_TTL_S = 10 * 60
SEAT_MAX_S = 12 * 3600
MIRROR_TTL_S = 7200  # the session state's TTL

# The kinds of entries in `sess:{sid}:ctl`.
CTL_SEATS = "seats"  # the seat list changed: read the mirror again
CTL_NOTE = "note"  # a guest's note for the host


@dataclass
class SeatRecord:
    seat_id: str
    guest_id: str
    name: str
    status: str
    capabilities: list[str] = field(default_factory=lambda: ["view"])
    rev: int = 1
    host_name: str = ""  # as the guest sees the host (the account's name at the invite)
    expires_at: float = 0.0  # epoch seconds; an open seat past it counts as expired

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "SeatRecord":
        d: dict[str, Any] = json.loads(raw)
        return cls(
            seat_id=str(d["seat_id"]),
            guest_id=str(d["guest_id"]),
            name=str(d.get("name", "")),
            status=str(d["status"]),
            capabilities=[str(c) for c in d.get("capabilities", ["view"])],
            rev=int(d.get("rev", 1)),
            host_name=str(d.get("host_name", "")),
            expires_at=float(d.get("expires_at", 0.0)),
        )

    @property
    def effective_status(self) -> str:
        """The status, with an open seat past its expiry counted as expired."""
        if self.status in OPEN and self.expires_at and self.expires_at < time.time():
            return "expired"
        return self.status

    @property
    def admitted(self) -> bool:
        return self.effective_status == "admitted"

    def may(self, capability: str) -> bool:
        """An admitted seat holds this capability, and this release honours it."""
        return self.admitted and capability in self.capabilities and capability in GRANTABLE

    def info(self) -> dict[str, Any]:
        """As the host sees it (protocol SeatInfo)."""
        return {
            "seat_id": self.seat_id,
            "name": self.name,
            "status": self.effective_status,
            "capabilities": self.capabilities,
        }


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def read_all(client: Any, sid: UUID | str) -> dict[str, SeatRecord]:
    raw = await client.hgetall(keys.sess_seats(sid))
    out: dict[str, SeatRecord] = {}
    for k, v in raw.items():
        try:
            out[_s(k)] = SeatRecord.from_json(v)
        except (ValueError, KeyError, TypeError):
            continue
    return out


async def read_one(client: Any, sid: UUID | str, seat_id: UUID | str) -> SeatRecord | None:
    raw = await client.hget(keys.sess_seats(sid), str(seat_id))
    if raw is None:
        return None
    try:
        return SeatRecord.from_json(raw)
    except (ValueError, KeyError, TypeError):
        return None


async def publish(client: Any, sid: UUID | str, record: SeatRecord, ttl_s: int) -> None:
    """Mirror one seat and tell the host's session, in one round trip."""
    async with client.pipeline(transaction=False) as pipe:
        pipe.hset(keys.sess_seats(sid), record.seat_id, record.to_json())
        pipe.expire(keys.sess_seats(sid), ttl_s)
        pipe.xadd(keys.sess_ctl(sid), {"k": CTL_SEATS}, maxlen=1000, approximate=True)
        pipe.expire(keys.sess_ctl(sid), ttl_s)
        await pipe.execute()
