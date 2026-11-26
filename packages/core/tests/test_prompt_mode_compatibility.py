"""Payload compatibility for the mode-aware prompt change; no provider calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMRequest

BASE_SOURCE_SHA256 = "9011a3d0e302be357e7c4ce60008b027a038e4147efb0d8b32d935077c8a2289"
BASE_COMMIT = "5081d573dacf175de89cebc3c396d1dcccf066ec"
FIXTURE = Path(__file__).parent / "fixtures" / "prompt_mode_payloads_v1.json"
OLD_READINESS = (
    "Leave out the tooling's bookkeeping (what is committed or not, counts of"
    " files and tests, review reruns, flags) and the inner detail of a defect,"
    " unless {user_name} is asked for exactly that."
)
NEW_READINESS = (
    "Leave out file/test counts, routine commit bookkeeping, and the inner"
    " detail of a defect unless {user_name} is asked for exactly that. Keep"
    " unresolved review, approval, security, or other conditions when they"
    " change release readiness, the target date, or the next action; put these"
    " before a recap of completed work."
)
CALL = prompts.CallContext(
    recent="Jamie: I checked staging. Alex: I reviewed the cache behavior.",
    summary="Alex described cache maintenance; Jamie owns the staging check.",
    answers="Q: What did you do? Alex: I reviewed cache eviction settings.",
    suggestions="A previous generated suggestion claimed a rollout; it is not user evidence.",
)
MEMORY = ("[n:m1] Alex maintains the cache; independent review remains open.",)
QUESTION = "Describe the cache work and its next step."


@dataclass(frozen=True)
class Case:
    pass_name: Literal["fast", "deep", "adaptive"]
    mode: str
    company: str | None
    language: str = "en"
    depth: str = "standard"
    expert: bool = False
    style: str = "bullets"
    length: str = "default"

    @property
    def id(self) -> str:
        return ":".join(
            (
                self.pass_name,
                self.mode,
                "company" if self.company else "no-company",
                self.language,
                self.depth,
                "expert" if self.expert else "primary",
                self.style,
                self.length,
            )
        )


def cases() -> tuple[Case, ...]:
    result: list[Case] = []
    lengths = ("default", "concise", "detailed")

    def add(case: Case) -> None:
        result.append(replace(case, length=lengths[len(result) % len(lengths)]))

    for mode in ("interview", "standup", "meeting", "other"):
        for language in ("en", "es"):
            add(Case("fast", mode, "Cedar", language=language))
            for style in ("bullets", "paragraph"):
                add(Case("deep", mode, "Cedar", language=language, style=style))
    for mode in ("standup", "meeting", "other"):
        for depth in ("brief", "standard", "narrative", "expert"):
            for expert in (False, True):
                for style in ("bullets", "paragraph"):
                    add(Case("adaptive", mode, "Cedar", depth=depth, expert=expert, style=style))
    for mode in ("standup", "meeting"):
        for expert in (False, True):
            for style in ("bullets", "paragraph"):
                add(
                    Case(
                        "adaptive",
                        mode,
                        None,
                        language="es",
                        depth="narrative" if expert else "brief",
                        expert=expert,
                        style=style,
                    )
                )
    return tuple(result)


CASES = cases()


def context(case: Case) -> prompts.PromptContext:
    return prompts.PromptContext(
        user_name="Alex",
        mode=case.mode,
        digest_text="[n:d1] Alex maintained Cedar's Redis cache.",
        pack_text="[r:p1] Staff engineer role: reliable systems and mentoring.",
        company=case.company,
        role="Engineer",
        company_digest="[c:c1] Cedar requires independent release review.",
        spoken_language=case.language,
    )


def request_for(case: Case) -> LLMRequest:
    ctx = context(case)
    qtype = "standup_status" if case.mode == "standup" else "technical"
    if case.pass_name == "fast":
        request = prompts.fast_pass(ctx, CALL, QUESTION, qtype, 250, MEMORY)
    elif case.pass_name == "deep":
        request = prompts.deep_pass(
            ctx,
            CALL,
            QUESTION,
            qtype,
            800,
            MEMORY,
            shown="Existing legacy cue.",
            style=case.style,
        )
    else:
        request = prompts.adaptive_pass(
            ctx,
            CALL,
            QUESTION,
            qtype,
            1200,
            MEMORY,
            depth=case.depth,
            expert=case.expert,
            style=case.style,
        )
    return prompts.with_response_length(request, case.length)


def fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def input_document() -> dict[str, object]:
    return {
        "cases": [asdict(case) for case in CASES],
        "contexts": [asdict(context(case)) for case in CASES],
        "call": asdict(CALL),
        "memory": MEMORY,
        "question": QUESTION,
        "max_tokens": {"fast": 250, "deep": 800, "adaptive": 1200},
        "legacy_shown": "Existing legacy cue.",
        "qtype": {"standup": "standup_status", "otherwise": "technical"},
    }


@pytest.fixture(scope="module")
def baseline_payloads() -> dict[str, str]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["baseline_source_sha256"] == BASE_SOURCE_SHA256
    assert data["baseline_commit"] == BASE_COMMIT
    assert data["input_sha256"] == fingerprint(input_document())
    rows = data["payload_sha256"]
    assert isinstance(rows, dict)
    assert len(CASES) == 80
    assert len({case.id for case in CASES}) == 80
    assert set(rows) == {case.id for case in CASES}
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in rows.items())
    return rows


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_legacy_and_strict_payload_compatibility(
    case: Case,
    baseline_payloads: dict[str, str],
) -> None:
    request = request_for(case)
    if case.pass_name == "adaptive" and case.mode == "standup" and case.company:
        # This is the only intended strict-mode change. Reverse exactly that one clause
        # before comparing the entire request, including evidence, preferences and budgets.
        old = OLD_READINESS.format(user_name="Alex")
        new = NEW_READINESS.format(user_name="Alex")
        system = request.system[0]
        assert system.text.count(new) == 1
        assert old not in system.text
        request = replace(
            request,
            system=[
                replace(system, text=system.text.replace(new, old)),
                *request.system[1:],
            ],
        )
    assert fingerprint(asdict(request)) == baseline_payloads[case.id]
