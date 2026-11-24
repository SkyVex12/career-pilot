"""Durable connected-time measurement; estimates only, never payment collection.

The gateway supplies server-observed connected intervals. PostgreSQL serializes
each session, deduplicates event IDs and subtracts already accepted time. Reconnect
gaps are never filled. Accepted segments and the session's payer/price snapshot
are append-only; deleting interview content has no effect on these records.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from copilot_core.db import Conn

MICROSECONDS_PER_HOUR = 3_600_000_000


@dataclass(frozen=True)
class UsagePrice:
    currency: str = "USD"
    rate_minor_per_hour: int = 500
    version: str = "live-usd-v1"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be a three-letter uppercase currency code")
        if type(self.rate_minor_per_hour) is not int or not 0 <= self.rate_minor_per_hour < 2**63:
            raise ValueError("hourly rate must be a nonnegative integer in currency minor units")
        if not self.version or len(self.version) > 128:
            raise ValueError("price version must contain 1 to 128 characters")


@dataclass(frozen=True)
class RecordedUsage:
    event_id: UUID
    recorded_microseconds: int
    duplicate: bool


@dataclass(frozen=True)
class UsagePriceTotal:
    currency: str
    price_version: str
    rate_minor_per_hour: int
    connected_microseconds: int
    amount_minor_numerator: int
    amount_minor_denominator: int = MICROSECONDS_PER_HOUR


@dataclass(frozen=True)
class UsageCurrencyTotal:
    currency: str
    estimated_minor: int  # Includes adjustments; round once per reporting currency.
    adjustment_minor: int


@dataclass(frozen=True)
class UsageDayTotal:
    day: date
    connected_microseconds: int
    session_count: int


@dataclass(frozen=True)
class UsageSummary:
    connected_microseconds: int
    session_count: int
    prices: tuple[UsagePriceTotal, ...]
    costs: tuple[UsageCurrencyTotal, ...]
    days: tuple[UsageDayTotal, ...]
    last_recorded_at: datetime | None


@dataclass(frozen=True)
class UsageTotals:
    connected_microseconds: int
    session_count: int
    session_connected_microseconds: int | None
    last_connected_at: datetime | None


@dataclass(frozen=True)
class LegacyUsageDayTotal:
    day: date
    estimated_microseconds: int
    session_count: int


@dataclass(frozen=True)
class LegacyUsageSummary:
    estimated_microseconds: int
    session_count: int
    days: tuple[LegacyUsageDayTotal, ...]


@dataclass(frozen=True)
class DailyUsage:
    workspace_id: UUID
    measured_microseconds: int
    legacy_microseconds: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("usage timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _bounds(started_at: datetime, ended_at: datetime) -> tuple[datetime, datetime]:
    start, end = _utc(started_at), _utc(ended_at)
    if end <= start:
        raise ValueError("usage interval must have a positive duration")
    return start, end


def duration_microseconds(started_at: datetime, ended_at: datetime) -> int:
    """Exact timedelta conversion without a float or per-checkpoint rounding."""
    delta = ended_at - started_at
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def split_utc_days(started_at: datetime, ended_at: datetime) -> list[tuple[datetime, datetime]]:
    start, end = _bounds(started_at, ended_at)
    parts: list[tuple[datetime, datetime]] = []
    while start < end:
        midnight = datetime.combine(start.date() + timedelta(days=1), time.min, UTC)
        stop = min(midnight, end)
        parts.append((start, stop))
        start = stop
    return parts


def _round_minor(numerator: int) -> int:
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) + MICROSECONDS_PER_HOUR // 2) // MICROSECONDS_PER_HOUR)


async def usage_session(conn: Conn, *, session_id: UUID) -> dict[str, Any] | None:
    """Recover a previously pinned payer/price when the transient cache expires."""
    row = await conn.fetchrow(
        "select session_id, workspace_id, payer_account_id, currency, rate_minor_per_hour,"
        " price_version from app.live_usage_sessions where session_id=$1",
        session_id,
    )
    return dict(row) if row is not None else None


async def ensure_usage_session(
    conn: Conn,
    *,
    workspace_id: UUID,
    session_id: UUID,
    payer_account_id: UUID,
    price: UsagePrice,
) -> None:
    """Pin the authenticated server-side attribution before accepting usage.

    Must run within the caller's workspace transaction. Runtime column grants make
    payer, workspace and price immutable once created, including for worker roles.
    """
    await conn.execute(
        "insert into app.usage_prices(version,currency,rate_minor_per_hour) values($1,$2,$3)"
        " on conflict do nothing",
        price.version,
        price.currency,
        price.rate_minor_per_hour,
    )
    configured = await conn.fetchrow(
        "select currency,rate_minor_per_hour from app.usage_prices where version=$1",
        price.version,
    )
    if configured is None or tuple(configured.values()) != (
        price.currency,
        price.rate_minor_per_hour,
    ):
        raise ValueError("price version is already pinned to a different currency or rate")
    # The FK-supporting composite unique index also conflicts during concurrent
    # first inserts. Handle every unique constraint, then validate the pinned row.
    await conn.execute(
        "insert into app.live_usage_sessions(session_id,workspace_id,payer_account_id,"
        " currency,rate_minor_per_hour,price_version) values($1,$2,$3,$4,$5,$6)"
        " on conflict do nothing",
        session_id,
        workspace_id,
        payer_account_id,
        price.currency,
        price.rate_minor_per_hour,
        price.version,
    )
    row = await conn.fetchrow(
        "select workspace_id,payer_account_id,currency,rate_minor_per_hour,price_version"
        " from app.live_usage_sessions where session_id=$1 for update",
        session_id,
    )
    expected = (
        workspace_id,
        payer_account_id,
        price.currency,
        price.rate_minor_per_hour,
        price.version,
    )
    if row is None or tuple(row.values()) != expected:
        raise ValueError("usage session attribution or price does not match its immutable snapshot")


async def record_interval(
    conn: Conn,
    *,
    workspace_id: UUID,
    session_id: UUID,
    payer_account_id: UUID,
    event_id: UUID,
    started_at: datetime,
    ended_at: datetime,
    price: UsagePrice,
) -> RecordedUsage:
    """Accept only uncovered time, atomically; retries cannot increase usage.

    Different IDs and partially overlapping reconnect/process intervals are also
    deduplicated. The caller uses user_tx(workspace_id); the nested transaction
    protects all inserts if a malformed event or conflicting event ID is rejected.
    """
    start, end = _bounds(started_at, ended_at)
    async with conn.transaction():
        await ensure_usage_session(
            conn,
            workspace_id=workspace_id,
            session_id=session_id,
            payer_account_id=payer_account_id,
            price=price,
        )
        previous = await conn.fetchrow(
            "select session_id,workspace_id,payer_account_id,started_at,ended_at,"
            " recorded_microseconds from app.live_usage_events where event_id=$1",
            event_id,
        )
        if previous is not None:
            if tuple(previous.values())[:5] != (
                session_id,
                workspace_id,
                payer_account_id,
                start,
                end,
            ):
                raise ValueError("usage event ID was reused with different content")
            return RecordedUsage(event_id, previous["recorded_microseconds"], True)
        uncovered = await conn.fetch(
            "select lower(r) as started_at,upper(r) as ended_at"
            " from app.live_usage_sessions,"
            " unnest(tstzmultirange(tstzrange($2,$3,'[)'))-covered) as r"
            " where session_id=$1",
            session_id,
            start,
            end,
        )
        parts = [
            part for row in uncovered for part in split_utc_days(row["started_at"], row["ended_at"])
        ]
        measured = sum(duration_microseconds(a, b) for a, b in parts)
        await conn.execute(
            "insert into app.live_usage_events(event_id,session_id,workspace_id,payer_account_id,"
            " started_at,ended_at,recorded_microseconds) values($1,$2,$3,$4,$5,$6,$7)",
            event_id,
            session_id,
            workspace_id,
            payer_account_id,
            start,
            end,
            measured,
        )
        if parts:
            await conn.executemany(
                "insert into app.live_usage_intervals(event_id,part,session_id,workspace_id,"
                " payer_account_id,started_at,ended_at) values($1,$2,$3,$4,$5,$6,$7)",
                [
                    (event_id, i, session_id, workspace_id, payer_account_id, a, b)
                    for i, (a, b) in enumerate(parts)
                ],
            )
        await conn.execute(
            "update app.live_usage_sessions set covered=covered +"
            " tstzmultirange(tstzrange($2,$3,'[)')) where session_id=$1",
            session_id,
            start,
            end,
        )
    return RecordedUsage(event_id, measured, False)


async def session_microseconds(conn: Conn, *, session_id: UUID) -> int:
    return int(
        await conn.fetchval(
            "select coalesce(sum(duration_microseconds),0) from app.live_usage_intervals"
            " where session_id=$1",
            session_id,
        )
    )


async def usage_totals(
    conn: Conn, *, payer_account_id: UUID, session_id: UUID | None = None
) -> UsageTotals | None:
    """One snapshot of confirmed lifetime and optional session time for an account.

    ``covered`` is updated atomically with accepted intervals, merging adjacent
    checkpoints. Polling an active call therefore reads its connected ranges rather
    than every checkpoint ever written. It never extrapolates a running interval or
    adds legacy wall-clock estimates. A missing or foreign requested session returns
    None, including when its workspace is shared with the authenticated account.
    """
    row = await conn.fetchrow(
        "select coalesce(sum(t.duration),0) as duration,"
        " count(*) filter(where t.duration>0) as sessions,"
        " coalesce(sum(t.duration) filter(where s.session_id=$2),0) as session_duration,"
        " coalesce(bool_or(s.session_id=$2),false) as session_found,"
        " max(t.last_connected_at) as last_connected_at"
        " from app.live_usage_sessions s left join lateral ("
        " select sum((extract(epoch from upper(r)-lower(r))*1000000)::bigint) as duration,"
        " max(upper(r)) as last_connected_at from unnest(s.covered) as r"
        " ) t on true where s.payer_account_id=$1",
        payer_account_id,
        session_id,
    )
    assert row is not None
    if session_id is not None and not row["session_found"]:
        return None
    return UsageTotals(
        int(row["duration"]),
        row["sessions"],
        int(row["session_duration"]) if session_id is not None else None,
        row["last_connected_at"],
    )


async def record_adjustment(
    conn: Conn,
    *,
    adjustment_id: UUID,
    workspace_id: UUID,
    session_id: UUID,
    payer_account_id: UUID,
    amount_minor: int,
    reason: str,
    effective_at: datetime,
) -> bool:
    """Append a signed correction (negative is a credit); never rewrite usage.

    Returns false for an identical retry. No public payment/adjustment endpoint is
    exposed; only a trusted workspace-scoped worker can call this helper.
    """
    if type(amount_minor) is not int or amount_minor == 0 or abs(amount_minor) >= 2**63:
        raise ValueError("adjustment must be a nonzero integer in currency minor units")
    if not reason or len(reason) > 1024:
        raise ValueError("adjustment reason must contain 1 to 1024 characters")
    at = _utc(effective_at)
    expected = (session_id, workspace_id, payer_account_id, amount_minor, reason, at)
    async with conn.transaction():
        row = await conn.fetchrow(
            "insert into app.usage_adjustments(adjustment_id,session_id,workspace_id,"
            " payer_account_id,amount_minor,reason,effective_at) values($1,$2,$3,$4,$5,$6,$7)"
            " on conflict(adjustment_id) do nothing returning adjustment_id",
            adjustment_id,
            *expected,
        )
        if row is not None:
            return True
        old = await conn.fetchrow(
            "select session_id,workspace_id,payer_account_id,amount_minor,reason,effective_at"
            " from app.usage_adjustments where adjustment_id=$1",
            adjustment_id,
        )
        if old is None or tuple(old.values()) != expected:
            raise ValueError("adjustment ID was reused with different content")
    return False


async def usage_summary(
    conn: Conn,
    *,
    payer_account_id: UUID,
    started_at: datetime,
    ended_at: datetime,
) -> UsageSummary:
    """Payer-scoped measured usage for a half-open UTC reporting window.

    Cost numerators remain integers across all checkpoints. Round once per currency
    for this estimate; finalized invoicing and payment collection are separate work.
    """
    start, end = _bounds(started_at, ended_at)
    rows = await conn.fetch(
        "select s.currency,s.price_version,s.rate_minor_per_hour,"
        " count(distinct i.session_id) as sessions,max(e.recorded_at) as recorded_at,"
        " sum((extract(epoch from least(i.ended_at,$3)-greatest(i.started_at,$2))*1000000)"
        " ::bigint) as duration from app.live_usage_intervals i"
        " join app.live_usage_sessions s using(session_id,workspace_id,payer_account_id)"
        " join app.live_usage_events e using(event_id,session_id,workspace_id,payer_account_id)"
        " where i.payer_account_id=$1 and i.day>=($2 at time zone 'UTC')::date"
        " and i.day<=($3 at time zone 'UTC')::date and i.started_at<$3 and i.ended_at>$2"
        " group by s.currency,s.price_version,s.rate_minor_per_hour",
        payer_account_id,
        start,
        end,
    )
    prices: dict[tuple[str, str, int], int] = {}
    sessions = 0
    last: datetime | None = None
    for row in rows:
        key = (row["currency"], row["price_version"], row["rate_minor_per_hour"])
        us = int(row["duration"])
        prices[key] = prices.get(key, 0) + us
        sessions += row["sessions"]
        last = max(last, row["recorded_at"]) if last else row["recorded_at"]
    totals = tuple(UsagePriceTotal(*key, us, us * key[2]) for key, us in sorted(prices.items()))
    numerators: dict[str, int] = {}
    for total in totals:
        numerators[total.currency] = (
            numerators.get(total.currency, 0) + total.amount_minor_numerator
        )
    adjustments = {
        row["currency"]: int(row["amount"])
        for row in await conn.fetch(
            "select s.currency,sum(a.amount_minor) as amount from app.usage_adjustments a"
            " join app.live_usage_sessions s using(session_id,workspace_id,payer_account_id)"
            " where a.payer_account_id=$1 and a.effective_at>=$2 and a.effective_at<$3"
            " group by s.currency",
            payer_account_id,
            start,
            end,
        )
    }
    costs = tuple(
        UsageCurrencyTotal(
            c, _round_minor(numerators.get(c, 0)) + adjustments.get(c, 0), adjustments.get(c, 0)
        )
        for c in sorted(numerators.keys() | adjustments.keys())
    )
    day_rows = await conn.fetch(
        "select day,count(distinct session_id) as sessions,"
        " sum((extract(epoch from least(ended_at,$3)-greatest(started_at,$2))*1000000)"
        " ::bigint) as duration from app.live_usage_intervals"
        " where payer_account_id=$1 and day>=($2 at time zone 'UTC')::date"
        " and day<=($3 at time zone 'UTC')::date and started_at<$3 and ended_at>$2"
        " group by day order by day",
        payer_account_id,
        start,
        end,
    )
    return UsageSummary(
        sum(prices.values()),
        sessions,
        totals,
        costs,
        tuple(UsageDayTotal(r["day"], int(r["duration"]), r["sessions"]) for r in day_rows),
        last,
    )


async def legacy_summary(
    conn: Conn,
    *,
    workspace_id: UUID,
    started_at: datetime,
    ended_at: datetime,
) -> LegacyUsageSummary:
    """Unpriced historical estimates; their synthetic ends do not prove connection."""
    start, end = _bounds(started_at, ended_at)
    rows = await conn.fetch(
        "select session_id,greatest(started_at,$2) as started_at,least(ended_at,$3) as ended_at"
        " from app.legacy_usage_estimates where workspace_id=$1 and started_at<$3"
        " and ended_at>$2",
        workspace_id,
        start,
        end,
    )
    days: dict[date, tuple[int, set[UUID]]] = {}
    for row in rows:
        for a, b in split_utc_days(row["started_at"], row["ended_at"]):
            total, ids = days.get(a.date(), (0, set()))
            ids.add(row["session_id"])
            days[a.date()] = (total + duration_microseconds(a, b), ids)
    return LegacyUsageSummary(
        sum(us for us, _ in days.values()),
        len(rows),
        tuple(LegacyUsageDayTotal(day, us, len(ids)) for day, (us, ids) in sorted(days.items())),
    )


async def daily_usage(conn: Conn, *, day: date) -> list[DailyUsage]:
    """System reporting compatibility; this rollup is not a billing authority."""
    start = datetime.combine(day, time.min, UTC)
    end = start + timedelta(days=1)
    rows = await conn.fetch(
        "select workspace_id,sum(measured) as measured,sum(legacy) as legacy from ("
        " select workspace_id,sum(duration_microseconds) as measured,0::numeric as legacy"
        " from app.live_usage_intervals where day=$1 group by workspace_id union all"
        " select workspace_id,0::numeric,sum((extract(epoch from"
        " least(ended_at,$3)-greatest(started_at,$2))*1000000)::bigint)"
        " from app.legacy_usage_estimates where started_at<$3 and ended_at>$2"
        " group by workspace_id) x group by workspace_id",
        day,
        start,
        end,
    )
    return [
        DailyUsage(row["workspace_id"], int(row["measured"]), int(row["legacy"])) for row in rows
    ]
