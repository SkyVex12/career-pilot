"""scripts/dev_profile.py: the digest and pack it builds match the documented formats."""

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from copilot_core.packs import SHORT_ID, cited_refs, ref_map

ROOT = Path(__file__).resolve().parent.parent


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "dev_profile", ROOT / "scripts" / "dev_profile.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_profile"] = module
    spec.loader.exec_module(module)
    return module


EXTRACTED: dict[str, Any] = {
    "user_name": "Alex Doe",
    "roles": [
        {
            "key": "r1",
            "company": "Acme Payments",
            "title": "Senior Backend Engineer",
            "start": "2021",
            "end": "now",
            "team": "Payments Platform",
            "summary": "Owns the ledger.",
        }
    ],
    "projects": [
        {
            "key": "p1",
            "role_key": "r1",
            "title": "Ledger migration",
            "summary": "Moved the ledger to a new store.",
            "stack": ["Go", "PostgreSQL"],
            "metrics": ["p95 latency down 40%"],
        }
    ],
    "stories": [
        {
            "key": "s1",
            "project_key": "p1",
            "title": "Payments migration",
            "one_liner": "Zero-downtime ledger move.",
            "situation": "Ledger at capacity.",
            "task": "Move it live.",
            "action": "Dual writes and a shadow read path.",
            "result": "p95 latency down 40%",
            "competencies": ["ownership"],
        }
    ],
    "skills": [{"name": "Python", "years": 9}, {"name": "Kafka", "years": 0}],
    "job": {"company": "Globex", "role_title": "Staff Engineer", "job_summary": "Payments."},
    "ranked_story_keys": ["s1"],
    "prepared_answers": [
        {
            "question": "Tell me about a system you scaled.",
            "talking_points": ["Moved the ledger with zero downtime [s1]", "Go and Postgres [p1]"],
        }
    ],
}


def test_build_digest_and_pack() -> None:
    script = load_script()
    uid = uuid.uuid4()
    digest, pack, labels = script.build(EXTRACTED, uid, "technical")
    assert digest.text.startswith("# Career digest (version ")
    assert "## Roles" in digest.text
    assert "## Top stories" in digest.text
    assert "Python (9 y), Kafka" in digest.text
    assert digest.version == digest.version_of(digest.text)
    # Every short id in the digest resolves to a node.
    assert set(SHORT_ID.findall(digest.text)) == set(digest.refs)
    assert len(labels) == 3
    assert pack.user_id == uid
    assert pack.mode == "interview"
    assert pack.digest_version == digest.version
    assert pack.context.company == "Globex"
    assert pack.context.round == "technical"
    assert [s.title for s in pack.ranked_stories] == ["Payments migration"]
    points = pack.prepared_answers[0].talking_points
    assert all(re.search(r"\[n:[0-9a-f]{4,}\]", p) for p in points)
    assert len(pack.prepared_answers[0].node_ids) == 2
    refs = ref_map(digest, pack)
    assert [r.label for r in cited_refs(" ".join(points), refs)] == [
        "Payments migration",
        "Ledger migration",
    ]
    # Round trip through the JSON the gateway reads.
    again = type(pack).model_validate_json(pack.model_dump_json(by_alias=True))
    assert again.pack_id == pack.pack_id
    assert '"schema":"session_pack.v1"' in pack.model_dump_json(by_alias=True)
