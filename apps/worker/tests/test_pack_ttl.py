"""How long a pack is kept (copilot_worker.packs.pack_ttl)."""

from datetime import UTC, datetime, timedelta

from copilot_worker.packs import PACK_TTL_MAX_S, pack_ttl
from copilot_worker.prompts import WORKLOG_SYSTEM

DAY = 24 * 3600


def test_an_applications_pack_outlives_the_day_it_was_built() -> None:
    # 2026-11-02 and 2026-11-05: interviews on applications from 2026-10-29 started with no
    # pack, since it had expired after 24 hours.
    now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
    assert pack_ttl(DAY, now, now, application=True) == PACK_TTL_MAX_S == 30 * DAY
    # A standup's pack still lives a day past its due time, within the same bounds.
    assert pack_ttl(DAY, now, now, application=False) == DAY
    assert pack_ttl(DAY, now + timedelta(hours=3), now, application=False) == DAY + 3 * 3600
    assert pack_ttl(DAY, now + timedelta(days=90), now, application=False) == PACK_TTL_MAX_S
    assert pack_ttl(DAY, now - timedelta(days=3), now, application=False) == DAY


def test_work_log_extraction_leaves_out_tool_bookkeeping() -> None:
    # 20 of 158 real work items carried commit state, file and test counts or review reruns,
    # and standup cards repeated them to a tester.
    assert "counts of files and tests" in WORKLOG_SYSTEM
    assert "what a teammate would be told" in WORKLOG_SYSTEM
