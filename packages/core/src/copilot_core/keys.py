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


def pack(uid: Id, pack_id: Id) -> str:
    return f"pack:{{{uid}}}:{{{pack_id}}}"


def pack_index(uid: Id) -> str:
    """Hash from `job:{job_id}`, `mode:{mode}` or `employer:{employer_id}:{mode}` to the pack
    id to load at session start."""
    return f"pack:{{{uid}}}:index"


def qa(uid: Id, qhash: str) -> str:
    return f"qa:{{{uid}}}:{{{qhash}}}"


def rate_limit(uid: Id, window: str) -> str:
    return f"rl:{{{uid}}}:{{{window}}}"


def employer_pack_field(employer_id: Id, mode: str) -> str:
    """The `pack:{uid}:index` field of a company's standup or meeting pack."""
    return f"employer:{employer_id}:{mode}"
