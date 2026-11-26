"""Representative generation cases from public paraphrases and explicitly fictional edges.

Only question and authorized source context enter requests. Reference facts, regex checks,
expected answers, and previous generated outputs are deliberately not copied.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def representative_cases(base: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    cases = [dict(case) for case in base]
    profile = tomllib.loads((root / "evals/bench/profile.toml").read_text("utf-8"))
    knowledge = tomllib.loads((root / "evals/knowledge/questions.toml").read_text("utf-8"))
    memory = [item["title"] + ": " + item["summary"] for item in knowledge["item"]]
    for question in knowledge["question"]:
        source = question["source"]
        documents: list[str] = []
        if source.endswith(".md"):
            directory = (root / "evals/knowledge/docs").resolve()
            path = (directory / source).resolve()
            if path.parent != directory:
                raise ValueError("Source document must remain in the authorized fixture directory")
            documents.append(path.read_text("utf-8"))
        cases.append(
            {
                "id": "knowledge-" + question["id"],
                "question": question["text"],
                "type": question["types"][0],
                "category": "standup_technical",
                "mode": "standup",
                "digest": (
                    "Alex Rivera is a backend engineer working on reports "
                    "at fictional Fernhill Logistics."
                ),
                "recent": "\n".join(
                    ("Alex" if role == "me" else "Dana") + ": " + text
                    for role, text in question.get("context", [])
                ),
                "memory": documents + memory,
                "refs": {},
                "depth": None,
                "source": (
                    "historical technical-standup question paraphrase; invented company documents"
                    if 1 <= question.get("turn", 0) <= 35
                    else "synthetic missing-context coverage"
                ),
            }
        )
    additional = tomllib.loads((root / "evals/answers/questions.toml").read_text("utf-8"))
    for question in additional["question"]:
        cases.append(
            {
                "id": "coverage-" + question["id"],
                "question": question["question"],
                "type": question["type"],
                "category": question["type"],
                "mode": "interview",
                "digest": profile["digest"],
                "recent": additional["transcript"],
                "memory": [],
                "refs": profile["refs"],
                "depth": None,
                "source": (
                    "existing fictional answer-quality coverage; reference answer checks withheld"
                ),
            }
        )
    handoff = tomllib.loads((root / "evals/sessions/standup_handoff.toml").read_text("utf-8"))
    recent: list[str] = []
    memory = [item["title"] + ": " + item["summary"] for item in handoff["item"]]
    for index, line in enumerate(handoff["line"]):
        if line.get("expect") == "answer" and line.get("addressee") == "me":
            cases.append(
                {
                    "id": f"handoff-{index}",
                    "question": line["text"],
                    "type": line["type"],
                    "category": "standup",
                    "mode": "standup",
                    "digest": "Alex Rivera is a backend engineer at fictional Fernhill Logistics.",
                    "recent": "\n".join(recent[-8:]),
                    "memory": memory,
                    "refs": {},
                    "depth": None,
                    "source": (
                        "historical standup handoff paraphrase; fixed fictional source context"
                    ),
                }
            )
        recent.append(("Alex" if line["speaker"] == "me" else "Dana") + ": " + line["text"])
    edges: list[tuple[str, str, str, str, str, list[str]]] = [
        (
            "location-missing",
            "Where are you based?",
            "other",
            "simple",
            "Alex is a backend engineer. No location is supplied.",
            [],
        ),
        (
            "salary-missing",
            "What compensation are you targeting?",
            "other",
            "simple",
            "Alex is a backend engineer. No compensation preference is supplied.",
            [],
        ),
        (
            "negative-experience",
            "Have you used Epic in production?",
            "technical",
            "technical",
            (
                "Alex has used Python, PostgreSQL and Redis. "
                "The supplied profile does not mention Epic."
            ),
            [],
        ),
        (
            "concrete-incident",
            "Describe the production incident you handled and your role.",
            "behavioral",
            "behavioral",
            (
                "Alex investigated a retry storm in March. "
                "Alex found missing request deduplication, "
                "added an idempotency key, and reviewed the rollback with Maya. "
                "The record contains no measured percentage improvement."
            ),
            [],
        ),
        (
            "bounded-design",
            (
                "Design a notification service for 200 requests per second. "
                "Email delivery may take a minute; duplicate messages must be avoided. "
                "Explain the key trade-off."
            ),
            "system_design",
            "system_design",
            profile["digest"],
            [],
        ),
        (
            "staging-not-production",
            "Is the retry fix already in production?",
            "standup_status",
            "standup",
            "Alex owns retry logging.",
            ["Retry fix passed staging on Tuesday; production release is planned for Thursday."],
        ),
        (
            "different-project",
            "Did you finish testing the carrier preview?",
            "standup_status",
            "standup",
            "Alex owns both carrier preview and retry logging.",
            [
                "Retry logging passed staging tests.",
                "Carrier preview was sent to Sam for review; no test result is recorded.",
            ],
        ),
        (
            "correction-context",
            "So the deployment was Monday, correct?",
            "respond",
            "simple",
            "Alex coordinates the release.",
            [
                "An earlier plan listed Monday. "
                "The latest release note says deployment occurred Wednesday."
            ],
        ),
    ]
    for identifier, question, kind, category, digest, memory in edges:
        cases.append(
            {
                "id": "edge-" + identifier,
                "question": question,
                "type": kind,
                "category": category,
                "mode": "standup" if category == "standup" else "interview",
                "digest": digest,
                "recent": "",
                "memory": memory,
                "refs": {},
                "depth": None,
                "source": "explicit fictional grounding/complexity edge case",
            }
        )
    return cases
