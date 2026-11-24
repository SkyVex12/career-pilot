"""Real PG16 metering invariants, scopes, retries, and content-deletion independence."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.migrate import apply
from copilot_core.teststack import MIGRATIONS, admin_url, as_role, temp_database
from copilot_core.usage import (
    UsagePrice,
    daily_usage,
    legacy_summary,
    record_adjustment,
    record_interval,
    session_microseconds,
    usage_summary,
)

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]
START = datetime(2026, 11, 30, 23, 59, 50, tzinfo=UTC)
PRICE = UsagePrice()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def url() -> AsyncIterator[str]:
    async with temp_database("copilot_usage") as value:
        yield value


@asynccontextmanager
async def scoped(url: str, role: str, scope: UUID) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(as_role(url, role))
    try:
        async with conn.transaction():
            await conn.execute(
                "select set_config('app.user_id',$1,true),set_config('app.account_id',$1,true)",
                str(scope),
            )
            yield conn
    finally:
        await conn.close()


async def add(
    conn: asyncpg.Connection,
    workspace: UUID,
    payer: UUID,
    session: UUID,
    start: datetime,
    end: datetime,
    *,
    event: UUID | None = None,
    price: UsagePrice = PRICE,
):
    return await record_interval(
        conn,
        workspace_id=workspace,
        payer_account_id=payer,
        session_id=session,
        event_id=event or uuid4(),
        started_at=start,
        ended_at=end,
        price=price,
    )


async def test_retries_overlaps_gaps_and_utc_month_boundary(url: str) -> None:
    workspace, payer, session, event = (uuid4() for _ in range(4))
    async with scoped(url, "app_worker", workspace) as conn:
        first = await add(
            conn, workspace, payer, session, START, START + timedelta(seconds=20), event=event
        )
        assert first.recorded_microseconds == 20_000_000
        retry = await add(
            conn, workspace, payer, session, START, START + timedelta(seconds=20), event=event
        )
        assert retry.duplicate
        assert retry.recorded_microseconds == 20_000_000
        overlap = await add(
            conn,
            workspace,
            payer,
            session,
            START + timedelta(seconds=15),
            START + timedelta(seconds=30),
        )
        assert overlap.recorded_microseconds == 10_000_000
        duplicate_time = await add(
            conn,
            workspace,
            payer,
            session,
            START + timedelta(seconds=2),
            START + timedelta(seconds=25),
        )
        assert duplicate_time.recorded_microseconds == 0
        await add(
            conn,
            workspace,
            payer,
            session,
            START + timedelta(seconds=40),
            START + timedelta(seconds=50, microseconds=123),
        )
        assert await session_microseconds(conn, session_id=session) == 40_000_123
    async with scoped(url, "app_api", payer) as conn:
        nov = await usage_summary(
            conn,
            payer_account_id=payer,
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
            ended_at=datetime(2026, 12, 1, tzinfo=UTC),
        )
        dec = await usage_summary(
            conn,
            payer_account_id=payer,
            started_at=datetime(2026, 12, 1, tzinfo=UTC),
            ended_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert nov.connected_microseconds == 10_000_000
        assert dec.connected_microseconds == 30_000_123
        assert nov.session_count == dec.session_count == 1
        assert nov.days[0].connected_microseconds == 10_000_000
        assert dec.prices[0].amount_minor_numerator == 30_000_123 * 500


async def test_event_identity_and_price_attribution_cannot_change(url: str) -> None:
    workspace, payer, session, event = (uuid4() for _ in range(4))
    async with scoped(url, "app_worker", workspace) as conn:
        await add(
            conn, workspace, payer, session, START, START + timedelta(seconds=10), event=event
        )
        with pytest.raises(ValueError, match="different content"):
            await add(
                conn, workspace, payer, session, START, START + timedelta(seconds=11), event=event
            )
        with pytest.raises(ValueError, match="immutable snapshot"):
            await add(conn, workspace, uuid4(), session, START, START + timedelta(seconds=10))
        with pytest.raises(ValueError, match="immutable snapshot"):
            await add(
                conn,
                workspace,
                payer,
                session,
                START,
                START + timedelta(seconds=10),
                price=UsagePrice(version="new-usd-v2", rate_minor_per_hour=300),
            )
        with pytest.raises(ValueError, match="price version"):
            await add(
                conn,
                workspace,
                payer,
                uuid4(),
                START,
                START + timedelta(seconds=10),
                price=UsagePrice(rate_minor_per_hour=300),
            )
        assert await session_microseconds(conn, session_id=session) == 10_000_000


@pytest.mark.parametrize("new_price", [False, True])
async def test_concurrent_processes_cannot_double_count(url: str, new_price: bool) -> None:
    workspace, payer, session = (uuid4() for _ in range(3))
    price = UsagePrice(version=f"concurrent-{uuid4()}") if new_price else PRICE
    if not new_price:
        async with scoped(url, "app_worker", workspace) as conn:
            await conn.execute(
                "insert into app.usage_prices(version,currency,rate_minor_per_hour)"
                " values($1,$2,$3) on conflict do nothing",
                price.version,
                price.currency,
                price.rate_minor_per_hour,
            )
    # Ensure all connections reach their first insert together. Both the price
    # and header have composite unique indexes supporting their foreign keys.
    ready = asyncio.Barrier(6)

    async def write(start: int, end: int) -> int:
        async with scoped(url, "app_worker", workspace) as conn:
            await ready.wait()
            result = await add(
                conn,
                workspace,
                payer,
                session,
                START + timedelta(seconds=start),
                START + timedelta(seconds=end),
                price=price,
            )
            return result.recorded_microseconds

    amounts = await asyncio.gather(*(write(a, b) for a, b in [(0, 20), (10, 30), (5, 25)] * 2))
    assert sum(amounts) == 30_000_000
    async with scoped(url, "app_worker", workspace) as conn:
        assert await session_microseconds(conn, session_id=session) == 30_000_000


async def test_round_once_and_append_only_adjustments(url: str) -> None:
    workspace, payer, session, adjustment = (uuid4() for _ in range(4))
    async with scoped(url, "app_worker", workspace) as conn:
        for second in range(4):
            await add(
                conn,
                workspace,
                payer,
                session,
                START + timedelta(seconds=second),
                START + timedelta(seconds=second + 1),
            )

        async def adjust(amount: int) -> bool:
            return await record_adjustment(
                conn,
                adjustment_id=adjustment,
                workspace_id=workspace,
                session_id=session,
                payer_account_id=payer,
                amount_minor=amount,
                reason="Service interruption",
                effective_at=START,
            )

        assert await adjust(-1)
        assert not await adjust(-1)
        with pytest.raises(ValueError, match="different content"):
            await adjust(-2)
    async with scoped(url, "app_api", payer) as conn:
        summary = await usage_summary(
            conn, payer_account_id=payer, started_at=START, ended_at=START + timedelta(minutes=1)
        )
        assert summary.prices[0].amount_minor_numerator == 4_000_000 * 500
        assert summary.costs[0].estimated_minor == 0  # 0.555... -> 1 cent, then -1 credit.
        assert summary.costs[0].adjustment_minor == -1


async def test_payer_reads_across_workspaces_and_worker_writes_are_scoped(url: str) -> None:
    workspace, other, payer, session = (uuid4() for _ in range(4))
    async with scoped(url, "app_worker", workspace) as conn:
        await add(conn, workspace, payer, session, START, START + timedelta(seconds=10))
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("update app.live_usage_sessions set rate_minor_per_hour=1")
        for table in ("live_usage_events", "live_usage_intervals", "usage_adjustments"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(f"delete from app.{table}")
    async with scoped(url, "app_worker", other) as conn:
        assert await session_microseconds(conn, session_id=session) == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await add(conn, workspace, payer, uuid4(), START, START + timedelta(seconds=1))
    async with scoped(url, "app_api", workspace) as conn:
        assert await conn.fetchval("select count(*) from app.live_usage_sessions") == 0
    async with scoped(url, "app_api", payer) as conn:
        assert await conn.fetchval("select count(*) from app.live_usage_sessions") == 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("delete from app.live_usage_sessions")
    conn = await asyncpg.connect(as_role(url, "app_api"))
    try:
        assert await conn.fetchval("select count(*) from app.live_usage_intervals") == 0
    finally:
        await conn.close()


async def test_legacy_estimates_and_measured_time_survive_history_deletion(url: str) -> None:
    workspace, payer, legacy, measured = (uuid4() for _ in range(4))
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(
            "insert into app.users(id,email,display_name) values($1,$2,'Test')",
            workspace,
            f"{workspace}@example.com",
        )
        await admin.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at,"
            " billed_seconds) values($1,$2,'interview',false,$3,$4,20)",
            legacy,
            workspace,
            START,
            START + timedelta(seconds=20),
        )
        await admin.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at,"
            " billed_seconds,metering_version) values($1,$2,'interview',false,$3,$4,60,"
            " 'connected-v1')",
            measured,
            workspace,
            START,
            START + timedelta(seconds=60),
        )
        async with scoped(url, "app_worker", workspace) as conn:
            await add(conn, workspace, payer, measured, START, START + timedelta(seconds=5))
        await admin.execute("delete from app.sessions where user_id=$1", workspace)
        async with scoped(url, "app_api", workspace) as conn:
            old = await legacy_summary(
                conn,
                workspace_id=workspace,
                started_at=START,
                ended_at=START + timedelta(minutes=2),
            )
            assert old.estimated_microseconds == 20_000_000
            assert old.session_count == 1
            assert len(old.days) == 2
            row = await conn.fetchrow("select * from app.legacy_usage_estimates")
            assert row is not None
            assert row["payer_account_id"] is None
            assert not row["billable"]
        await admin.execute("delete from app.users where id=$1", workspace)
        async with scoped(url, "app_api", payer) as conn:
            current = await usage_summary(
                conn,
                payer_account_id=payer,
                started_at=START,
                ended_at=START + timedelta(minutes=2),
            )
            assert current.connected_microseconds == 5_000_000
        day_rows = await daily_usage(admin, day=START.date())
        ours = next(r for r in day_rows if r.workspace_id == workspace)
        assert ours.measured_microseconds == 5_000_000
        assert ours.legacy_microseconds == 10_000_000
    finally:
        await admin.close()


async def test_all_customer_ledger_tables_have_forced_rls(url: str) -> None:
    admin = await asyncpg.connect(url)
    try:
        rows = await admin.fetch(
            "select relname,relrowsecurity,relforcerowsecurity from pg_class"
            " where oid in ('app.live_usage_sessions'::regclass,'app.live_usage_events'::regclass,"
            " 'app.live_usage_intervals'::regclass,'app.usage_adjustments'::regclass,"
            " 'app.legacy_usage_estimates'::regclass)"
        )
        assert len(rows) == 5
        assert all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows)
    finally:
        await admin.close()


async def test_backfill_is_unpriced_and_does_not_guess_shared_workspace_payer(
    tmp_path: Path,
) -> None:
    # Apply exactly the pre-ledger schema, insert retained history, then upgrade.
    for path in MIGRATIONS.glob("*.sql"):
        if path.name < "0029":
            (tmp_path / path.name).write_bytes(path.read_bytes())
    admin = await asyncpg.connect(admin_url())
    name = f"copilot_usage_upgrade_{uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin_url())._replace(path=f"/{name}"))
    try:
        await apply(url, tmp_path)
        conn = await asyncpg.connect(url)
        workspace, session, owner = (uuid4() for _ in range(3))
        try:
            for uid in (owner, workspace):
                await conn.execute(
                    "insert into app.users(id,email,display_name,owner_id) values($1,$2,'Test',$3)",
                    uid,
                    f"{uid}@example.com",
                    owner if uid == workspace else None,
                )
            await conn.execute(
                "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at,"
                " billed_seconds) values($1,$2,'interview',false,$3,$4,60)",
                session,
                workspace,
                START,
                START + timedelta(minutes=1),
            )
            await apply(url, MIGRATIONS)
            row = await conn.fetchrow("select * from app.legacy_usage_estimates")
            assert row is not None
            assert row["session_id"] == session
            assert row["duration_microseconds"] == 60_000_000
            assert row["payer_account_id"] is None
            assert row["billable"] is False
            assert await conn.fetchval("select count(*) from app.live_usage_sessions") == 0
            assert await conn.fetchval("select count(*) from app.usage_prices") == 0
        finally:
            await conn.close()
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()
