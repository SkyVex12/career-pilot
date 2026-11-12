"""Storing a Career-OS link (ARCHITECTURE.md, "Career-OS link").

One place writes `app.integrations` for Career-OS, whichever way the link was approved: a code
typed under Settings (`routes/career_os.py`) or a sign-in on Career-OS
(`copilot_api.careeros_signin`). Both store exactly what `POST /v1/career-pilot/link` answered:
the read-only token, sealed with the account's data key, and whose it is.
"""

import json
from typing import Any
from uuid import UUID

from copilot_core import careeros
from copilot_core.crypto import UserCrypto
from copilot_core.db import Conn

PROVIDER = careeros.PROVIDER
ACCOUNT_TYPES = ("user", "admin", "super_admin")


def integration_row_id(uid: UUID) -> str:
    return f"{uid}:{PROVIDER}"


def account_type(linked: dict[str, Any]) -> str:
    kind = linked.get("account_type")
    return str(kind) if kind in ACCOUNT_TYPES else "user"


async def write(crypto: UserCrypto, conn: Conn, uid: UUID, linked: dict[str, Any]) -> str:
    """Make `linked` (Career-OS's answer to the exchange) the account's link, in the account's
    transaction, replacing any earlier one. Returns the link's account type."""
    cos_user: dict[str, Any] = linked["user"]
    kind = account_type(linked)
    secret = json.dumps({"token": linked["token"], "link_id": linked.get("link_id")})
    sealed, key_version = await crypto.encrypt(
        conn, uid, "integrations", integration_row_id(uid), secret
    )
    await conn.execute(
        "insert into app.integrations (user_id, provider, token_enc, key_version, scopes,"
        " status, external_id, external_label, external_kind, connected_at)"
        " values ($1, $2, $3, $4, $5, 'active', $6, $7, $8, now())"
        " on conflict (user_id, provider) do update set token_enc = excluded.token_enc,"
        " key_version = excluded.key_version, scopes = excluded.scopes,"
        " status = 'active', external_id = excluded.external_id,"
        " external_label = excluded.external_label,"
        " external_kind = excluded.external_kind, connected_at = now(),"
        " last_sync_at = null",
        uid, PROVIDER, sealed, key_version,
        [str(s) for s in linked.get("scopes") or ["applications:read"]],
        str(cos_user.get("id")), str(cos_user.get("name") or "")[:200], kind,
    )  # fmt: skip
    return kind


async def token_of(crypto: UserCrypto, conn: Conn, uid: UUID) -> str | None:
    """The account's link token, or None when it has no active link."""
    row = await conn.fetchrow(
        "select token_enc, key_version, status from app.integrations where provider = $1",
        PROVIDER,
    )
    if row is None or row["status"] != "active":
        return None
    sealed = await crypto.decrypt(
        conn, uid, "integrations", integration_row_id(uid), bytes(row["token_enc"]),
        int(row["key_version"]),
    )  # fmt: skip
    return str(json.loads(sealed)["token"])
