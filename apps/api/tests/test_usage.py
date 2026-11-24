"""Payer-owned, durable usage estimates independent of deletable call history."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from copilot_api.testing import Api
from copilot_core import keys, profiles, usage
from copilot_core.auth import Principal

pytestmark = pytest.mark.stack


async def measured_interval(
    api: Api,
    workspace_id: UUID,
    payer_account_id: UUID,
    start: datetime,
    end: datetime,
    *,
    rate: int = 500,
    version: str = "live-usd-v1",
) -> UUID:
    sid = uuid4()
    await api.admin.execute(
        "insert into app.sessions"
        " (id, user_id, mode, store_transcript, started_at, ended_at, metering_version)"
        " values ($1, $2, 'interview', false, $3, $4, 'connected-v1')",
        sid,
        workspace_id,
        start,
        end,
    )
    async with api.admin.transaction():
        await api.admin.execute("select set_config('app.user_id', $1, true)", str(workspace_id))
        await usage.record_interval(
            api.admin,
            workspace_id=workspace_id,
            session_id=sid,
            payer_account_id=payer_account_id,
            event_id=uuid4(),
            started_at=start,
            ended_at=end,
            price=usage.UsagePrice(currency="USD", rate_minor_per_hour=rate, version=version),
        )
    return sid


@pytest.mark.parametrize(
    "month", ["2026-1", "202601", "2026-13", "2026-00", "2026-01-01", "", "0000-01", "9999-12"]
)
async def test_usage_rejects_noncanonical_or_unrepresentable_months(api: Api, month: str) -> None:
    _, headers = api.user()
    response = await api.client.get("/v1/usage", headers=headers, params={"month": month})
    assert response.status_code == 422


async def test_usage_survives_history_deletion_and_legacy_never_adds_cost(api: Api) -> None:
    uid, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    start = datetime(2026, 10, 12, 12, tzinfo=UTC)
    measured = await measured_interval(api, uid, uid, start, start + timedelta(minutes=10))
    legacy = uuid4()
    await api.admin.execute(
        "insert into app.sessions"
        " (id, user_id, mode, store_transcript, started_at, ended_at, billed_seconds)"
        " values ($1, $2, 'interview', false, $3, $4, 300)",
        legacy,
        uid,
        start,
        start + timedelta(minutes=5),
    )
    response = await api.client.get("/v1/usage?month=2026-10", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" not in response.headers
    before = response.json()
    assert before["tracking_only"] is True
    assert before["legacy_billable"] is False
    assert (before["measured_seconds"], before["legacy_estimated_seconds"]) == (600, 300)
    assert (before["live_seconds"], before["live_minutes"], before["sessions"]) == (900, 15, 2)
    assert before["estimated_costs"] == [
        {"currency": "USD", "estimated_minor": 83, "adjustment_minor": 0}
    ]
    assert before["last_recorded_at"] is not None
    assert before["days"] == [
        {
            "day": "2026-10-12",
            "live_seconds": 900,
            "measured_seconds": 600,
            "legacy_estimated_seconds": 300,
        }
    ]
    for sid in (measured, legacy):
        deleted = await api.client.delete(f"/v1/sessions/{sid}", headers=headers)
        assert deleted.status_code == 204, deleted.text
    after = (await api.client.get("/v1/usage?month=2026-10", headers=headers)).json()
    for field in (
        "measured_seconds",
        "legacy_estimated_seconds",
        "live_seconds",
        "sessions",
        "estimated_costs",
        "days",
    ):
        assert after[field] == before[field]


@pytest.mark.parametrize(
    ("row_phase", "pending_log", "held_lease"),
    [
        ("missing", True, False),
        ("unfinalized", True, False),
        ("finalized", True, False),
        ("finalized", False, True),
    ],
)
async def test_history_deletion_waits_for_final_usage_save(
    api: Api, row_phase: str, pending_log: bool, held_lease: bool
) -> None:
    uid, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    sid = uuid4()
    start = datetime(2026, 10, 12, 12, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    if row_phase != "missing":
        await api.admin.execute(
            "insert into app.sessions"
            " (id, user_id, mode, store_transcript, started_at, ended_at, metering_version)"
            " values ($1, $2, 'interview', false, $3, $4, 'connected-v1')",
            sid,
            uid,
            start,
            end if row_phase == "finalized" else None,
        )
    await api.svc.valkey.hset(
        keys.sess_state(sid),
        mapping={
            "user_id": str(uid),
            "status": "ended",
            "metering_version": "connected-v1",
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
        },
    )
    event_id = uuid4()
    if pending_log:
        await api.svc.valkey.xadd(
            keys.sess_log(sid),
            {
                "e": json.dumps(
                    {
                        "kind": "usage.interval",
                        "event_id": str(event_id),
                        "session_id": str(sid),
                        "payer_account_id": str(uid),
                        "started_at": start.isoformat(),
                        "ended_at": end.isoformat(),
                        "duration_ms": 10_000,
                        "metering_version": "connected-v1",
                        "service": "live",
                        "currency": "USD",
                        "price_version": "live-usd-v1",
                        "rate_minor_per_hour": 500,
                    }
                )
            },
        )
    if held_lease:
        await api.svc.valkey.set(keys.sess_lease(sid), "gateway-still-draining", ex=30)
    saved_state = await api.svc.valkey.hgetall(keys.sess_state(sid))
    saved_log = await api.svc.valkey.xrange(keys.sess_log(sid))
    response = await api.client.delete(f"/v1/sessions/{sid}", headers=headers)
    assert response.status_code == 409, response.text
    assert "usage is still saving" in response.json()["detail"]
    assert await api.svc.valkey.hgetall(keys.sess_state(sid)) == saved_state
    assert await api.svc.valkey.xrange(keys.sess_log(sid)) == saved_log
    row = await api.admin.fetchrow("select ended_at from app.sessions where id=$1", sid)
    if row_phase == "missing":
        assert row is None
    else:
        assert row is not None
        assert row["ended_at"] == (end if row_phase == "finalized" else None)
    if held_lease:
        assert await api.svc.valkey.exists(keys.sess_lease(sid))

    # Simulate the final worker commit and log acknowledgement, then gateway release.
    await api.admin.execute(
        "insert into app.sessions"
        " (id, user_id, mode, store_transcript, started_at, ended_at, metering_version)"
        " values ($1, $2, 'interview', false, $3, $4, 'connected-v1')"
        " on conflict (id) do update set ended_at=excluded.ended_at",
        sid,
        uid,
        start,
        end,
    )
    async with api.admin.transaction():
        await api.admin.execute("select set_config('app.user_id', $1, true)", str(uid))
        await usage.record_interval(
            api.admin,
            workspace_id=uid,
            session_id=sid,
            payer_account_id=uid,
            event_id=event_id,
            started_at=start,
            ended_at=end,
            price=usage.UsagePrice(),
        )
    await api.svc.valkey.delete(keys.sess_log(sid), keys.sess_lease(sid))
    response = await api.client.delete(f"/v1/sessions/{sid}", headers=headers)
    assert response.status_code == 204, response.text
    assert await api.admin.fetchval("select id from app.sessions where id=$1", sid) is None
    assert not await api.svc.valkey.exists(keys.sess_state(sid), keys.sess_log(sid))
    summary = (await api.client.get("/v1/usage?month=2026-10", headers=headers)).json()
    assert summary["measured_seconds"] == 10


async def test_usage_splits_at_utc_month_boundary_and_keeps_frozen_prices(api: Api) -> None:
    uid, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    boundary = datetime(2026, 11, 1, tzinfo=UTC)
    await measured_interval(
        api,
        uid,
        uid,
        boundary - timedelta(seconds=60),
        boundary + timedelta(seconds=60),
        rate=300,
        version="launch-usd-v1",
    )
    await measured_interval(
        api, uid, uid, boundary + timedelta(seconds=60), boundary + timedelta(seconds=120)
    )
    october = (await api.client.get("/v1/usage?month=2026-10", headers=headers)).json()
    november = (await api.client.get("/v1/usage?month=2026-11", headers=headers)).json()
    assert october["measured_seconds"] == 60
    assert november["measured_seconds"] == 120
    assert october["estimated_costs"][0]["estimated_minor"] == 5
    assert november["estimated_costs"][0]["estimated_minor"] == 13
    assert october["days"][0]["day"] == "2026-10-31"
    assert november["days"][0]["day"] == "2026-11-01"
    assert {p["price_version"]: p["rate_minor_per_hour"] for p in november["prices"]} == {
        "launch-usd-v1": 300,
        "live-usd-v1": 500,
    }


async def test_shared_workspace_does_not_grant_access_to_another_payers_usage(api: Api) -> None:
    first, first_headers = api.user("First payer")
    second, second_headers = api.user("Second payer")
    for headers in (first_headers, second_headers):
        await api.client.get("/v1/me", headers=headers)
    person = str(uuid4())
    workspace = await profiles.ensure(
        api.svc.pool, Principal(first, "First", None), person, "Shared"
    )
    assert (
        await profiles.ensure(api.svc.pool, Principal(second, "Second", None), person, "Shared")
        == workspace
    )
    start = datetime(2026, 10, 10, 12, tzinfo=UTC)
    await measured_interval(api, workspace, first, start, start + timedelta(minutes=1))
    await measured_interval(api, workspace, second, start, start + timedelta(minutes=10))
    for uid, headers, seconds in ((first, first_headers, 60), (second, second_headers, 600)):
        # A selected workspace or a forged payer query cannot change the authenticated payer.
        requested = second if uid == first else first
        response = await api.client.get(
            f"/v1/usage?month=2026-10&payer_account_id={requested}",
            headers={**headers, "x-copilot-profile": str(workspace)},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["measured_seconds"] == seconds
        assert data["sessions"] == 1
        assert data["legacy_estimated_seconds"] == 0


async def test_usage_keeps_one_snapshot_when_a_checkpoint_commits_between_aggregates(
    api: Api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    start = datetime(2026, 10, 12, 12, tzinfo=UTC)
    sid = await measured_interval(api, uid, uid, start, start + timedelta(minutes=1))
    original_fetch = asyncpg.Connection.fetch
    committed = False

    async def fetch(
        conn: asyncpg.Connection,
        query: str,
        *args: Any,
        **kwargs: Any,
    ) -> list[asyncpg.Record]:
        nonlocal committed
        rows = await original_fetch(conn, query, *args, **kwargs)
        if not committed and "group by s.currency,s.price_version,s.rate_minor_per_hour" in query:
            committed = True
            # A worker commits after the first aggregate has read its data, but
            # before adjustments/days are read on the API's separate connection.
            async with api.admin.transaction():
                await api.admin.execute("select set_config('app.user_id',$1,true)", str(uid))
                await usage.record_interval(
                    api.admin,
                    workspace_id=uid,
                    payer_account_id=uid,
                    session_id=sid,
                    event_id=uuid4(),
                    started_at=start + timedelta(minutes=1),
                    ended_at=start + timedelta(minutes=2),
                    price=usage.UsagePrice(),
                )
                await usage.record_adjustment(
                    api.admin,
                    adjustment_id=uuid4(),
                    workspace_id=uid,
                    payer_account_id=uid,
                    session_id=sid,
                    amount_minor=-1,
                    reason="Test service credit",
                    effective_at=start,
                )
        return rows

    monkeypatch.setattr(asyncpg.Connection, "fetch", fetch)
    before_response = await api.client.get("/v1/usage?month=2026-10", headers=headers)
    assert before_response.status_code == 200, before_response.text
    assert committed
    before = before_response.json()
    assert before["measured_seconds"] == 60
    assert sum(day["measured_seconds"] for day in before["days"]) == 60
    assert before["estimated_costs"] == [
        {"currency": "USD", "estimated_minor": 8, "adjustment_minor": 0}
    ]
    after_response = await api.client.get("/v1/usage?month=2026-10", headers=headers)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()
    assert after["measured_seconds"] == 120
    assert sum(day["measured_seconds"] for day in after["days"]) == 120
    assert after["estimated_costs"] == [
        {"currency": "USD", "estimated_minor": 16, "adjustment_minor": -1}
    ]
