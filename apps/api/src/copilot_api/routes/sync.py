"""Small scope-bound invalidation feed; content stays on its authorized resource routes."""

import base64
import json
import time
from typing import Any, Literal, cast

from fastapi import APIRouter, Response

from copilot_api.data_cache import revision
from copilot_api.deps import AccountDep, SvcDep, UserDep

router = APIRouter()
MAX_AGE_S = 30 * 24 * 3600


def encode_cursor(scope: list[str], after: int, until: int | None, issued: int) -> str:
    value = [1, *scope, after, until, issued]
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(
    cursor: str | None, scope: list[str], now: int
) -> tuple[int, int | None, int] | None:
    if not cursor or len(cursor) > 1024:
        return None
    try:
        parsed: Any = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(parsed, list):
            return None
        value = cast(list[Any], parsed)
        if (
            len(value) != 7
            or value[:4] != [1, *scope]
            or type(value[4]) is not int
            or value[4] < 0
            or (value[5] is not None and (type(value[5]) is not int or value[5] < value[4]))
            or type(value[6]) is not int
            or not 0 <= now - value[6] < MAX_AGE_S
        ):
            return None
        return value[4], value[5], value[6]
    except (ValueError, TypeError, UnicodeError):
        return None


@router.get("/sync")
async def changes(
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
    response: Response,
    domain: Literal["history", "work"],
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """No/expired/foreign cursor returns reset and a bootstrap watermark. Capture this
    before loading lists; then consume changes since it. Pages contain invalidations,
    not snapshots: refetch changed resources (404 means remove). A resource rewritten
    while paging will be delivered by the next watermark. No writes can commit below
    a watermark already observed, because scope revision increments serialize commits.
    """
    response.headers.update(
        {
            "Cache-Control": "private, no-store",
            "X-Copilot-Account": str(account.user_id),
            "X-Copilot-Workspace": str(user.user_id),
        }
    )
    now = int(time.time())
    scope = [str(account.user_id), str(user.user_id), domain]
    decoded = decode_cursor(cursor, scope, now)
    async with svc.tx(user) as conn:
        current = await revision(conn, user.user_id, domain)
        response.headers["X-Copilot-Revision"] = str(current)
        reset = decoded is None or decoded[0] > current
        after, until, issued = decoded or (current, None, now)
        until = min(until if until is not None else current, current)
        # A previously public session may have become account-private. Do not expose
        # its id to other accounts, and do not let their existing cached copy survive.
        if not reset and domain == "history":
            reset = bool(
                await conn.fetchval(
                    "select exists(select 1 from app.data_changes where user_id=$1 and domain=$2"
                    " and revision>$3 and revision<=$4 and private_account_id is not null"
                    " and private_account_id<>$5)",
                    user.user_id,
                    domain,
                    after,
                    until,
                    account.user_id,
                )
            )
        if reset:
            return {
                "reset": True,
                "revision": current,
                "changes": [],
                "has_more": False,
                "cursor": encode_cursor(scope, current, None, now),
            }
        rows = await conn.fetch(
            "select resource,resource_id,revision,deleted from app.data_changes"
            " where user_id=$1 and domain=$2 and revision>$3 and revision<=$4"
            " and (private_account_id is null or private_account_id=$5)"
            " order by revision limit $6",
            user.user_id,
            domain,
            after,
            until,
            account.user_id,
            max(1, min(limit, 500)) + 1,
        )
    size = max(1, min(limit, 500))
    more = len(rows) > size
    rows = rows[:size]
    last = int(rows[-1]["revision"]) if more else until
    return {
        "reset": False,
        "revision": until,
        "has_more": more,
        "cursor": encode_cursor(scope, last, until if more else None, issued if more else now),
        "changes": [
            {
                "resource": r["resource"],
                "id": r["resource_id"],
                "revision": r["revision"],
                "deleted": r["deleted"],
            }
            for r in rows
        ],
    }
