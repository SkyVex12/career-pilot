from __future__ import annotations

import json
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals import live_answer_schedule as installed_schedule
from copilot_evals import live_answer_schedule as schedule
from copilot_evals.live_answer_eval import fingerprint

from copilot_gateway.config import Settings

REPO = Path(__file__).resolve().parents[2]

EXECUTION = replay.schedule_run_identity(
    Namespace(max_cost=None),
    {
        "concurrency": 2,
        "timeout_s": 90,
        "seed": 20261125,
        "code": {"evals/src/copilot_evals/live_answer_replay.py": "a" * 64},
        "provider_configuration_sha256": "d" * 64,
        "git_head": "b" * 40,
        "git_diff_sha256": "c" * 64,
    },
)


@pytest.fixture
def inputs(tmp_path):
    cases = [
        {"id": "first", "question": "Where?", "context_sha256": "a"},
        {"id": "second", "question": "Why?", "context_sha256": "b"},
    ]
    mapping = {"baseline": "legacy_dual", "candidate": "adaptive_on_demand"}
    identities = {v: {"architecture": v, "model": "example"} for v in mapping.values()}
    manifest = {
        "schema": 2,
        "design": "frozen-paired-abba-v1",
        "case_sha256": fingerprint(cases),
        "condition_sha256": {k: fingerprint(identities[v]) for k, v in mapping.items()},
        "repetitions": 2,
        "execution": deepcopy(EXECUTION),
        "blocks": [
            {"case_id": "second", "rep": 0, "conditions": ["candidate", "baseline"]},
            {"case_id": "first", "rep": 1, "conditions": ["baseline", "candidate"]},
            {"case_id": "first", "rep": 0, "conditions": ["candidate", "baseline"]},
            {"case_id": "second", "rep": 1, "conditions": ["baseline", "candidate"]},
        ],
    }
    return tmp_path / "schedule.json", cases, mapping, identities, manifest


def invoke(inputs):
    path, cases, mapping, identities, manifest = inputs
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return schedule.explicit_case_blocks(path, cases, mapping, identities, 2, EXECUTION)


def test_exact_order_and_identity(inputs):
    blocks, identity = invoke(inputs)
    assert [(c["id"], name, rep) for block in blocks for c, name, rep in block] == [
        ("second", "adaptive_on_demand", 0),
        ("second", "legacy_dual", 0),
        ("first", "legacy_dual", 1),
        ("first", "adaptive_on_demand", 1),
        ("first", "adaptive_on_demand", 0),
        ("first", "legacy_dual", 0),
        ("second", "legacy_dual", 1),
        ("second", "adaptive_on_demand", 1),
    ]
    assert identity["manifest"] == inputs[-1]
    assert identity["manifest_sha256"] == fingerprint(inputs[-1])
    assert len(identity["source_sha256"]) == 64
    assert blocks[0][0][0] is inputs[1][1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", 3),
        ("design", ""),
        ("design", "x" * 201),
        ("design", []),
        ("case_sha256", "stale"),
        ("repetitions", True),
        ("repetitions", 3),
        ("condition_sha256", {}),
        ("blocks", None),
        ("blocks", []),
    ],
)
def test_bad_manifest_rejected(inputs, field, value):
    inputs[-1][field] = value
    with pytest.raises(ValueError, match=r"[Ee]xplicit schedule"):
        invoke(inputs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "absent"),
        ("case_id", []),
        ("rep", True),
        ("rep", -1),
        ("rep", 2),
        ("rep", "0"),
        ("conditions", ["baseline"]),
        ("conditions", ["baseline", "baseline"]),
        ("conditions", ["baseline", "other"]),
        ("conditions", ["baseline", {}]),
        ("conditions", "baseline"),
    ],
)
def test_bad_block_rejected(inputs, field, value):
    inputs[-1]["blocks"][0][field] = value
    with pytest.raises(ValueError, match=r"[Ee]xplicit schedule"):
        invoke(inputs)


def test_unknown_fields_rejected(inputs):
    inputs[-1]["unexpected"] = "value"
    with pytest.raises(ValueError, match=r"[Ee]xplicit schedule"):
        invoke(inputs)
    del inputs[-1]["unexpected"]
    inputs[-1]["blocks"][0]["unexpected"] = "value"
    with pytest.raises(ValueError, match=r"[Ee]xplicit schedule"):
        invoke(inputs)


def test_duplicate_cell_rejected(inputs):
    inputs[-1]["blocks"][1] = deepcopy(inputs[-1]["blocks"][0])
    with pytest.raises(ValueError, match=r"[Ee]xplicit schedule"):
        invoke(inputs)


def test_context_or_model_change_rejected(inputs):
    inputs[1][0]["context_sha256"] = "changed"
    with pytest.raises(ValueError, match="inputs"):
        invoke(inputs)
    inputs[-1]["case_sha256"] = fingerprint(inputs[1])
    inputs[3]["legacy_dual"]["model"] = "different"
    with pytest.raises(ValueError, match="settings"):
        invoke(inputs)


def test_raw_file_fingerprint_captures_whitespace(inputs):
    _, original = invoke(inputs)
    path, cases, mapping, identities, manifest = inputs
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _, modified = schedule.explicit_case_blocks(path, cases, mapping, identities, 2, EXECUTION)
    assert original["source_sha256"] != modified["source_sha256"]
    assert original["manifest_sha256"] == modified["manifest_sha256"]


async def test_explicit_block_sequence_preserved_under_bounded_workers(inputs):
    import asyncio

    blocks, _ = invoke(inputs)
    seen = []
    active = 0
    peak = 0

    async def run(case, condition, rep):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append((case["id"], condition, rep))
        await asyncio.sleep(0)
        active -= 1

    await replay.bounded_blocks(blocks, 2, run, lambda: False)
    assert peak == 2
    for block in blocks:
        expected = [(case["id"], condition, rep) for case, condition, rep in block]
        actual = [r for r in seen if r[0] == expected[0][0] and r[2] == expected[0][2]]
        assert actual == expected


async def test_offline_preflight_validates_schedule_before_any_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(
        installed_schedule, "explicit_case_blocks", schedule.explicit_case_blocks, raising=False
    )
    monkeypatch.setattr(replay, "root_directory", lambda: REPO)

    def forbidden(*args, **kwargs):
        raise AssertionError("Provider creation was forbidden")

    monkeypatch.setattr(replay, "make_providers", forbidden)
    cases = replay.load_cases()[:2]
    for case in cases:
        case.update(answer_style="bullets", response_length="default")
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases), encoding="utf-8")
    args = Namespace(
        cases=case_file,
        live=False,
        out=tmp_path / "unused",
        schedule_file=tmp_path / "schedule.json",
        reps=2,
        seed=10,
        architectures=["A", "D"],
        concurrency=2,
        timeout=90,
        max_cost=None,
    )
    settings = Settings(
        openai_detector_retry_tier=None,
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        openai_deep_service_tier=None,
    )
    conditions = replay.configured_conditions(args, settings, None)
    manifest = {
        "schema": 2,
        "design": "test-paired",
        "case_sha256": fingerprint(cases),
        "repetitions": 2,
        "execution": replay.schedule_run_identity(
            args,
            replay.experiment_identity(
                cases, settings, args.reps, args.seed, args.concurrency, args.timeout
            ),
        ),
        "condition_sha256": {
            v.label: fingerprint(v.identity(replay.CONFIG_FIELDS)) for v in conditions.values()
        },
        "blocks": [
            {"case_id": case["id"], "rep": rep, "conditions": ["D", "A"]}
            for rep in range(2)
            for case in reversed(cases)
        ],
    }
    args.schedule_file.write_text(json.dumps(manifest), encoding="utf-8")
    await replay.execute(args)
    assert not args.out.exists()
    manifest["case_sha256"] = "bad"
    args.schedule_file.write_text(json.dumps(manifest), encoding="utf-8")

    async def forbidden_preflight(*args, **kwargs):
        raise AssertionError("Invalid schedule passed into engine preflight")

    monkeypatch.setattr(replay, "preflight", forbidden_preflight)
    with pytest.raises(ValueError, match="inputs"):
        await replay.execute(args)
