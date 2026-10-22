"""Company workspace helpers that need no database (copilot_core.workplace)."""

import uuid
from datetime import date

from copilot_core.packs import render_pack
from copilot_core.workplace import Hit, latest_only, local_today, standup_pack


def hit(kind: str, text: str, day: str | None) -> Hit:
    return Hit(uuid.uuid4(), kind, text, day, 1.0)


def test_search_keeps_the_newest_entry_of_a_work_item() -> None:
    old = hit("work_item", "PSP-412: PR #881 opened, Priya reviewing", "2026-10-08")
    new = hit("work_item", "PSP-412: deployed to production", "2026-10-13")
    other = hit("decision", "PSP-412: idempotency key per refund", "2026-10-06")
    assert latest_only([old, other, new]) == [new, other]  # the newest, in the best place


def test_local_today_uses_the_users_time_zone() -> None:
    from datetime import UTC, datetime

    late = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    assert local_today({"timezone": "America/New_York"}, late) == date(2026, 10, 19)
    assert local_today({"timezone": "Mars/Base"}, late) == date(2026, 10, 20)


def test_monday_standup_reads_fridays_log() -> None:
    from copilot_core.packs import EmployerRef

    def node(title: str, day: date, state: str) -> dict[str, object]:
        return {"id": uuid.uuid4(), "kind": "work_item", "title": title, "summary": title,
                "body": {"state": state}, "valid_from": day}  # fmt: skip

    nodes = [
        node("Shipped the fix", date(2026, 10, 16), "done"),
        node("Start the backfill", date(2026, 10, 16), "planned"),
        node("Old task", date(2026, 10, 1), "done"),
    ]
    emp = EmployerRef(id=uuid.uuid4(), name="Larkspur")
    pack = standup_pack(uuid.uuid4(), emp, nodes, date(2026, 10, 19), "v1")
    assert pack.standup is not None
    assert pack.standup.yesterday_day == "2026-10-16"
    assert [i.text for i in pack.standup.yesterday] == ["Shipped the fix"]
    assert [i.text for i in pack.standup.today] == ["Start the backfill"]
    assert "Old task" not in render_pack(pack)
    assert {a.question for a in pack.prepared_answers} >= {"What did you work on yesterday?"}
