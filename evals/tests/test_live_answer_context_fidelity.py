import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from copilot_evals import live_answer_judge as judge
from copilot_evals import live_answer_replay as replay

from copilot_core import prompts
from copilot_core.llm import ModelRouter
from copilot_gateway.config import Settings


def base():
    return {
        "id": "fixture-context",
        "question": "What is the Atlas release status?",
        "type": "standup_status",
        "mode": "standup",
        "digest": "Fictional engineer Alex.",
        "recent": "Me: I own the review.",
        "memory": ["[n:aa] Atlas awaits approval."],
        "source": "authored fictional source; no generated answer",
    }


def full():
    return {
        **base(),
        "user_name": "Sam Chen",
        "spoken_language": "en",
        "company": "Alder Systems",
        "role": "Backend engineer",
        "company_digest": "Atlas is the export service; Boreal is billing.",
        "pack_text": "Atlas approval review belongs to Sam.",
        "pack_source": "source_facts_only",
        "call_summary": "Earlier discussion confirmed that production is paused.",
        "earlier_answers": "Question: Who owns the review?\nMe: I own the review.",
        "earlier_answers_source": "user_speech",
    }


def test_absent_fields_preserve_old_hash_and_full_prompt_bytes():
    case = base()
    a = {
        **deepcopy(case),
        "category": case["type"],
        "context_sha256": hashlib.sha256(
            json.dumps(
                {k: case[k] for k in ("digest", "recent", "memory")}, sort_keys=True
            ).encode()
        ).hexdigest(),
    }
    b = replay.validate_cases([deepcopy(case)])[0]
    assert a == b
    settings = Settings.model_construct()
    old = SimpleNamespace(
        prompt_context=lambda: prompts.PromptContext(
            "Alex Rivera", case["mode"], case["digest"], None
        ),
        call_context=lambda: prompts.CallContext(recent=case["recent"]),
    )
    new = replay.Host(settings, ModelRouter({}, 1.0), b)
    assert old.prompt_context() == new.prompt_context()
    assert old.call_context() == new.call_context()
    for factory, args in [
        (prompts.fast_pass, (None, 200)),
        (prompts.deep_pass, ("standup_status", 600)),
        (prompts.adaptive_pass, ("standup_status", 400)),
    ]:
        assert factory(
            old.prompt_context(), old.call_context(), case["question"], *args, case["memory"]
        ) == factory(
            new.prompt_context(), new.call_context(), case["question"], *args, case["memory"]
        )
    flags = replay.context_fidelity(b)
    assert flags["workplace_pack_missing"]
    assert not flags["workplace_rules_enabled"]


def test_full_context_is_exact_and_activates_workplace_rules():
    case = replay.validate_cases([full()])[0]
    host = replay.Host(Settings.model_construct(), ModelRouter({}, 1.0), case)
    ctx = host.prompt_context()
    call = host.call_context()
    assert ctx.at_work
    assert ctx.company == "Alder Systems"
    assert ctx.user_name == "Sam Chen"
    assert ctx.pack_text == case["pack_text"]
    assert ctx.company_digest == case["company_digest"]
    assert call.summary == case["call_summary"]
    assert call.answers == case["earlier_answers"]
    assert call.suggestions == ""
    req = prompts.deep_pass(ctx, call, case["question"], "standup_status", 600, case["memory"])
    combined = "\n".join(block.text for block in req.system) + str(req.content)
    for field in (
        "company",
        "role",
        "company_digest",
        "pack_text",
        "call_summary",
        "earlier_answers",
    ):
        assert case[field] in combined
    flags = replay.context_fidelity(case)
    assert flags["workplace_rules_enabled"]
    assert not flags["workplace_pack_missing"]
    assert flags["source_declarations_are_not_truth_verification"]


@pytest.mark.parametrize(
    "field",
    [
        "user_name",
        "spoken_language",
        "company",
        "role",
        "company_digest",
        "pack_text",
        "call_summary",
        "earlier_answers",
        "pack_source",
        "earlier_answers_source",
    ],
)
def test_each_explicit_context_input_is_hashed_and_mutation_rejected(field):
    case = replay.validate_cases([full()])[0]
    if field == "pack_source":
        case[field] = "unverified"
    elif field == "earlier_answers_source":
        case[field] = "model_suggestion"
    else:
        case[field] += " changed"
    with pytest.raises(ValueError, match=r"declared|Source context fingerprint changed"):
        replay.validate_cases([case])


@pytest.mark.parametrize("field", ["company", "pack_text", "call_summary", "earlier_answers"])
def test_nontext_context_is_rejected(field):
    case = base()
    case[field] = None
    with pytest.raises(ValueError, match="must be text"):
        replay.validate_cases([case])


@pytest.mark.parametrize(
    ("field", "value"),
    [("earlier_answers", "Previous generated answer"), ("pack_text", "Prepared model answer")],
)
def test_generated_history_cannot_enter_via_undeclared_full_context(field, value):
    case = base()
    case[field] = value
    with pytest.raises(ValueError, match="declared"):
        replay.validate_cases([case])


@pytest.mark.parametrize("field", ["suggestions", "earlier_suggestions", "fast", "deep", "answer"])
def test_historical_generated_answer_fields_remain_disallowed(field):
    case = base()
    case[field] = "Previous model output"
    with pytest.raises(ValueError, match="leakage"):
        replay.validate_cases([case])


def test_normalized_evidence_and_blind_payload_keep_full_context():
    case = replay.validate_cases([full()])[0]
    trial = {
        "id": case["id"],
        "question": case["question"],
        "architecture": "adaptive_on_demand",
        "rep": 0,
        "context_sha256": case["context_sha256"],
        "events": [],
        "metadata_logs": [],
        "status": "failed",
        "failure": "scripted offline fixture",
    }
    runs = replay.normalized_runs([trial], [case], {"strategies": {"D": "adaptive_on_demand"}}, [])
    row = runs[0].rows[0]
    evidence = row["evidence"]
    for name in replay.CONTEXT_TEXT_FIELDS:
        assert evidence[name] == case[name]
    assert row["context_fidelity"]["workplace_rules_enabled"]
    answer = {
        "initial": "",
        "expanded": "",
        "has_expansion": False,
        "boundary_units": [],
        "delivery": {},
    }
    item = {
        "question": case["question"],
        "category": "standup",
        "evidence": evidence,
        "A": answer,
        "B": answer,
    }
    payload = judge.blind_payload(item, ["B", "A"])
    for name in replay.CONTEXT_TEXT_FIELDS:
        assert payload["evidence"][name] == case[name]
    assert "source" not in payload["evidence"]
    assert "pack_source" not in payload["evidence"]


def test_interview_company_digest_unconsumed_fact_does_not_reach_judge():
    case = {**full(), "mode": "interview", "company_digest": "UNIQUE_UNCONSUMED_ZEPHYR_FACT"}
    case = replay.validate_cases([case])[0]
    host = replay.Host(Settings.model_construct(), ModelRouter({}, 1.0), case)
    req = prompts.adaptive_pass(
        host.prompt_context(), host.call_context(), case["question"], "other", 400, case["memory"]
    )
    assert "UNIQUE_UNCONSUMED_ZEPHYR_FACT" not in str(req)
    trial = {
        "id": case["id"],
        "question": case["question"],
        "architecture": "adaptive_on_demand",
        "rep": 0,
        "context_sha256": case["context_sha256"],
        "events": [],
        "metadata_logs": [],
        "status": "failed",
        "failure": "offline",
    }
    row = replay.normalized_runs([trial], [case], {"strategies": {"D": "adaptive_on_demand"}}, [])[
        0
    ].rows[0]
    assert all(key not in row["evidence"] for key in ("company", "role", "company_digest"))
    assert row["context_fidelity"]["unconsumed_declared_fields"] == [
        "company",
        "role",
        "company_digest",
    ]
