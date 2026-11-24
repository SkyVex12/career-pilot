from datetime import UTC, datetime, timedelta, timezone

import pytest

from copilot_core.usage import UsagePrice, duration_microseconds, split_utc_days


def test_day_split_is_exact_across_month_and_timezone() -> None:
    offset = timezone(timedelta(hours=-6))
    start = datetime(2026, 11, 30, 17, 59, 59, 123456, tzinfo=offset)
    end = start + timedelta(seconds=2, microseconds=654321)
    parts = split_utc_days(start, end)
    assert parts == [
        (datetime(2026, 11, 30, 23, 59, 59, 123456, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)),
        (datetime(2026, 12, 1, tzinfo=UTC), datetime(2026, 12, 1, 0, 0, 1, 777777, tzinfo=UTC)),
    ]
    assert sum(duration_microseconds(a, b) for a, b in parts) == 2_654_321


@pytest.mark.parametrize("minutes", [0, -1])
def test_nonpositive_intervals_are_rejected(minutes: int) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="positive"):
        split_utc_days(start, start + timedelta(minutes=minutes))


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        split_utc_days(datetime(2026, 1, 1), datetime(2026, 1, 2))


@pytest.mark.parametrize("rate", [-1, 1.5, True, 2**63])
def test_price_does_not_accept_float_money_or_invalid_rates(rate: int) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        UsagePrice(rate_minor_per_hour=rate)
