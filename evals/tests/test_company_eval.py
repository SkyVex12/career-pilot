"""The company-workspace eval's inputs (evals/company) and what each variant shows the model,
without model calls: the packs and the <memory> lines come from the same code as the gateway's."""

import json
import uuid
from datetime import date
from pathlib import Path

import pytest
from copilot_evals import company_eval as ce
from copilot_evals.bench import build_profile
from copilot_evals.benchdata import load_profile


def test_questions_and_company_material_load() -> None:
    questions, meeting = ce.load_questions()
    assert len(questions) == 20
    assert {q.mode for q in questions} == {"standup", "meeting"}
    assert all(q.key_points for q in questions)
    assert meeting["agenda"]
    company = ce.load_company()
    truth = ce.company_truth(company)
    assert all(log["day"] in truth for log in company["log"])
    assert all(doc["name"] in truth for doc in company["document"])
    # The standup's "yesterday" is the last log before the eval's day.
    days = sorted(str(log["day"]) for log in company["log"])
    assert days[-1] < str(company["today"])


def synthetic_memory(path: Path) -> None:
    day = "2026-10-19"
    nodes = [
        ("work_item", "PR #893 merged", "Batch-loads promo rules; p95 840 ms to 310 ms",
         {"state": "done"}, day),
        ("work_item", "Outbox in staging", "Blocked: no grant on the outbox table",
         {"state": "blocked"}, day),
        ("work_item", "Backfill script", "Start the backfill (CHK-1311)",
         {"state": "planned"}, day),
        ("decision", "Keep SQS FIFO for reservations", "Outbox instead of Kafka; revisit in Q1",
         {}, "2026-10-14"),
        ("system", "Reservation service", "Holds inventory; Go on ECS; SQS FIFO",
         {"stack": ["Go"]}, None),
        ("work_item", "Old audit", "Audited refund logs for PSP-300", {"state": "done"},
         "2026-08-01"),
    ]  # fmt: skip
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": str(uuid.uuid4()),
                        "kind": k,
                        "title": t,
                        "summary": s,
                        "body": b,
                        "source": {"type": "worklog"},
                        "valid_from": d,
                    }
                    for k, t, s, b, d in nodes
                ],
                "edges": [],
            }
        ),
        "utf-8",
    )


@pytest.mark.stack
async def test_after_shows_the_companys_pack_and_memory_before_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from copilot_core.migrate import DEFAULT_DATABASE_URL

    mem = tmp_path / "memory.json"
    synthetic_memory(mem)
    monkeypatch.setattr(ce, "MEMORY", mem)
    questions, meeting = ce.load_questions()
    by_id = {q.id: q for q in questions}
    digest, _ = build_profile(load_profile(), None, None, {})
    admin = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    async with ce.scratch_database(admin) as url:
        conn = await ce.connect(url)
        try:
            await ce.seed_company(conn, ce.load_company(), "Alex Rivera")
            assert await ce.load_memory(conn, None) is None
            packs = await ce.build_packs(conn, meeting, date(2026, 10, 20))
            update = by_id["su_update"]
            after = await ce.prepare_question(
                "after", update, digest, packs, conn, None, None, "fast"
            )
            before = await ce.prepare_question(
                "before", update, digest, packs, conn, None, None, "fast"
            )
            audit = by_id["mt_dup_events"]
            meet = await ce.prepare_question(
                "after", audit, digest, packs, conn, None, None, "deep"
            )
        finally:
            await conn.close()
    assert after.ctx.at_work
    assert after.pack_text is not None
    assert "Yesterday (2026-10-19):" in after.pack_text
    assert "PR #893 merged" in after.pack_text
    assert "Blocked: no grant" in after.pack_text
    assert "Backfill script" in after.pack_text.split("Today:")[1]
    assert not before.ctx.at_work
    assert before.pack_text is not None
    assert "PR #893" not in before.pack_text
    assert before.memory == []
    meeting_pack = packs["meeting"]
    assert meeting_pack.meeting is not None
    assert meeting_pack.meeting.agenda == meeting["agenda"]
    # A two-month-old item is not in the brief; asked about, search would bring it.
    assert "Old audit" not in (meet.pack_text or "")
    text = ce.grader_input("TRUTH", digest.text, meet, audit, "deep", "- an answer")
    assert "<company_truth>\nTRUTH" in text
    assert "Question (other): What caused the duplicate order events" in text
