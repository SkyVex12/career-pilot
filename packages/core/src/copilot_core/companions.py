"""Same-account display grants and idempotent control vocabulary; never capture ownership."""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from copilot_core import keys
from copilot_core.valkey_client import text

TTL_S = 12 * 3600
MAX_DISPLAYS = 5
CTL = "companion"


def records_key(sid: UUID | str) -> str:
    return f"sess:{{{sid}}}:companions"


def view_key(sid: UUID | str) -> str:
    return f"sess:{{{sid}}}:companion-view"


def command_key(sid: UUID | str, cid: UUID | str) -> str:
    return f"sess:{{{sid}}}:companion-command:{cid}"


@dataclass
class Attachment:
    attachment_id: str
    account_id: str
    profile_id: str
    session_id: str
    device_name: str
    expires_at: float
    revoked: bool = False

    def dump(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def load(cls, raw: str | bytes) -> "Attachment":
        return cls(**json.loads(raw))


def allowed(
    record: Attachment | None, state: dict[str, str], account_id: str, *, now: float | None = None
) -> bool:
    return bool(
        record is not None
        and not record.revoked
        and record.expires_at > (time.time() if now is None else now)
        and record.account_id == account_id == state.get("account_id")
        and record.profile_id == state.get("user_id")
        and state.get("private_context_account_id") in (None, "", account_id)
        and state.get("status") not in (None, "ended")
        and state.get("platform") != "mobile_in_person"
        and not state.get("client_version", "").startswith("mobile-")
    )


async def read(client: Any, sid: str, attachment_id: str) -> Attachment | None:
    raw = await client.hget(records_key(sid), attachment_id)
    if raw is None:
        return None
    try:
        record = Attachment.load(raw)
    except (TypeError, ValueError, KeyError):
        return None
    return record if record.session_id == sid and record.attachment_id == attachment_id else None


async def publish(client: Any, record: Attachment) -> None:
    async with client.pipeline(transaction=False) as pipe:
        pipe.hset(records_key(record.session_id), record.attachment_id, record.dump())
        pipe.expire(records_key(record.session_id), TTL_S)
        await pipe.execute()


class Join(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["companion.join"]
    session_id: UUID
    attachment_id: UUID
    last_seq: int = Field(default=0, ge=0)
    client_version: str = Field(default="mobile-0.0.0", max_length=100)
    access_token: str | None = Field(default=None, max_length=16384)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["companion.command"]
    command_id: UUID
    kind: Literal["answer", "followup", "end"]
    text: str | None = Field(default=None, max_length=4000)
    confirmed: bool = False
    summarize: bool = True

    @model_validator(mode="after")
    def validate_action(self) -> "Command":
        if self.kind == "followup" and not (self.text and self.text.strip()):
            raise ValueError("A follow-up needs text")
        if self.kind == "end" and not self.confirmed:
            raise ValueError("End call requires explicit confirmation")
        if self.kind != "followup" and self.text is not None:
            raise ValueError("Only follow-up accepts text")
        return self

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


# Atomic in the session's Valkey cluster slot. A retry cannot queue generation twice.
QUEUE = """
local old = redis.call('HGET', KEYS[1], 'digest')
if old then
  if old ~= ARGV[1] then return 'conflict' end
  return redis.call('HGET', KEYS[1], 'result') or 'queued'
end
redis.call('HSET', KEYS[1], 'digest', ARGV[1], 'result', 'queued')
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('XADD', KEYS[2], 'MAXLEN', '~', 1000, '*', 'k', 'companion', 'd', ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 'queued'
"""
CLAIM = """
if redis.call('HGET', KEYS[1], 'result') ~= 'queued' then return 0 end
redis.call('HSET', KEYS[1], 'result', 'uncertain')
return 1
"""


async def enqueue(client: Any, record: Attachment, command: Command) -> str:
    body = {
        "attachment_id": record.attachment_id,
        "account_id": record.account_id,
        "expires_at": time.time() + 30,
        "command": command.model_dump(mode="json"),
    }
    result = await client.eval(
        QUEUE,
        2,
        command_key(record.session_id, command.command_id),
        keys.sess_ctl(record.session_id),
        command.digest(),
        json.dumps(body),
        TTL_S,
    )
    return text(result)


async def claim(client: Any, sid: str, command_id: str) -> bool:
    return bool(await client.eval(CLAIM, 1, command_key(sid, command_id)))


async def finish(client: Any, sid: str, command_id: str, result: str) -> None:
    await client.hset(command_key(sid, command_id), "result", result)
