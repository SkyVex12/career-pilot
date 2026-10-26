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


def test_prepared_standup_points_are_short_names_not_summaries() -> None:
    # 2026-10-26: the prepared answer showed whole summaries, 150 to 300 characters each,
    # one narrating "The user requested ... but Claude suppressed a server warning".
    from copilot_core.packs import EmployerRef

    long = " ".join(["detail"] * 60)

    def node(title: str, day: date, state: str, ext: str = "") -> dict[str, object]:
        return {"id": uuid.uuid4(), "kind": "work_item", "title": title, "summary": long,
                "body": {"state": state, "external_id": ext}, "valid_from": day}  # fmt: skip

    friday = date(2026, 10, 23)
    nodes = [
        node("Dispatch queue alerts", friday, "done"),
        node("Staging data cleanup", friday, "done"),
        node("Label retry fix", friday, "done", "LAB-412"),
        node("Webhook signature check", friday, "done"),
        node("Rate cache rollout", friday, "planned"),
    ]
    emp = EmployerRef(id=uuid.uuid4(), name="Larkspur")
    pack = standup_pack(uuid.uuid4(), emp, nodes, date(2026, 10, 26), "v1")
    answers = {a.question: a.talking_points for a in pack.prepared_answers}
    yesterday = answers["What did you work on yesterday?"]
    assert len(yesterday) == 3
    assert yesterday[0].startswith("Done: Label retry fix (LAB-412) [")  # tracked first
    assert answers["What are you working on today?"][0].startswith("Next: Rate cache rollout")
    for points in answers.values():
        for p in points:
            assert "detail" not in p, p
            assert len(p.split()) <= 18, p
    assert pack.standup is not None
    assert long in pack.standup.yesterday[0].text  # the deep pass keeps the full item
