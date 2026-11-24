"""Valkey key names (ARCHITECTURE.md, "Valkey key design").

The braces are literal Valkey hash tags: every key of one session hashes to one slot, so a Lua
script may touch all of them in cluster mode. Keys of one user share that user's slot.
"""

from uuid import UUID

type Id = UUID | str


def sess_state(sid: Id) -> str:
    return f"sess:{{{sid}}}:state"


def sess_events(sid: Id) -> str:
    return f"sess:{{{sid}}}:events"


def sess_log(sid: Id) -> str:
    return f"sess:{{{sid}}}:log"


def sess_lease(sid: Id) -> str:
    return f"sess:{{{sid}}}:lease"


def user_active_session(uid: Id) -> str:
    return f"user:{{{uid}}}:active_session"


def digest(uid: Id) -> str:
    return f"digest:{{{uid}}}"


def resume_digest(uid: Id, resume_version_id: Id) -> str:
    """One resume version's hot digest. `digest(uid)` stays the base version's (standups,
    meetings, and gateways from before resume versions)."""
    return f"digest:{{{uid}}}:{{{resume_version_id}}}"


def resume_digest_repair(uid: Id, resume_revision_id: Id) -> str:
    """Short lease coalescing requests to restore a source revision's derived cache."""
    return f"digest:{{{uid}}}:repair:{resume_revision_id}"


def pack(uid: Id, pack_id: Id) -> str:
    return f"pack:{{{uid}}}:{{{pack_id}}}"


def pack_index(uid: Id) -> str:
    """Hash from `job:{application_id}`, `interview:{interview_id}`, `mode:{mode}` or
    `employer:{employer_id}:{mode}` to the pack id to load at session start."""
    return f"pack:{{{uid}}}:index"


def qa(uid: Id, qhash: str) -> str:
    return f"qa:{{{uid}}}:{{{qhash}}}"


def rate_limit(uid: Id, window: str) -> str:
    return f"rl:{{{uid}}}:{{{window}}}"


def employer_pack_field(employer_id: Id, mode: str) -> str:
    """The `pack:{uid}:index` field of a company's standup or meeting pack."""
    return f"employer:{employer_id}:{mode}"


def knowledge(uid: Id, employer_id: Id) -> str:
    """A company's document sections, opened for live calls (copilot_core.knowledge.LiveIndex)."""
    return f"kb:{{{uid}}}:{employer_id}"


def knowledge_ask(uid: Id, ask_id: Id) -> str:
    """The result of one "test a question" on a company's engineering context."""
    return f"kbask:{{{uid}}}:{ask_id}"


# Guest seats (ARCHITECTURE.md, "Guest seats"). All but the connection key are the session's,
# in its slot, so one blocking read can follow a session's events and command results.


def sess_seats(sid: Id) -> str:
    """Hash from seat id to the seat's live record (copilot_core.seats.SeatRecord), written by
    the API after each change of `app.session_seats`."""
    return f"sess:{{{sid}}}:seats"


def sess_ctl(sid: Id) -> str:
    """Stream into the host's session: seat changes from the API, guests' commands."""
    return f"sess:{{{sid}}}:ctl"


def sess_ctl_results(sid: Id) -> str:
    """Stream back to the seat relay: what became of each guest command."""
    return f"sess:{{{sid}}}:ctlres"


def sess_view(sid: Id) -> str:
    """The overlay as a guest sees it, folded (copilot_gateway.seatview), with its seq."""
    return f"sess:{{{sid}}}:view"


def seat_conn(seat_id: Id) -> str:
    """The id of the one relay connection serving a seat; a newer one takes it over."""
    return f"seat:{{{seat_id}}}:conn"
