"""Offline-only regression coverage for blind paid judging and conservative resume."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from copilot_evals import live_answer_judge as judge
from copilot_evals.live_answer_eval import review_content
from copilot_evals.sensei_judge import RUBRIC

from copilot_core.llm import LLMDone, LLMRequest, LLMUsage
from copilot_core.pricing import DEFAULT_TIERS, ModelPrice

MODEL = "gpt-6.2-sol"


def sample_review(count: int = 2) -> dict[str, Any]:
    answer = {
        "initial": "Based in Austin",
        "expanded": "I am based in Austin.",
        "sentences": ["Based in Austin"],
        "boundary_units": [
            {"id": "fast:1", "pass": "fast", "text": "Based in Austin"},
            {"id": "deep:1", "pass": "deep", "text": "I am based in Austin."},
        ],
        "has_expansion": True,
        "delivery": {"primary_complete": True, "expanded_complete": True},
    }
    return {
        "schema": 2,
        "instructions": "UNTRUSTED old review instructions",
        "items": [
            {
                "pair_id": f"private-pair-{i}",
                "question": "Where are you based?",
                "category": "simple",
                "evidence": {"digest": "Candidate lives in Austin.", "source": "SECRET provenance"},
                "A": deepcopy(answer),
                "B": deepcopy(answer),
            }
            for i in range(count)
        ],
    }


def valid_response(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "preference": "tie",
        "notes": "Both deliver the location supported by the profile.",
        "evidence_refs": ["evidence.digest", "A.initial", "B.initial"],
        "uncertainty": None,
    }
    for side in ("A", "B"):
        units = payload[side]["boundary_units"]
        result[side] = {
            "first_useful_cue": units[0]["id"] if units else None,
            "first_useful_complete_sentence": units[-1]["id"] if units else None,
            "first_useful_cue_assessed": True,
            "first_useful_complete_sentence_assessed": True,
            "unit_grammar": [
                {"unit_id": unit["id"], "complete": unit["pass"] == "deep"} for unit in units
            ],
            "scores": dict.fromkeys(RUBRIC),
            "redundant": True if payload[side]["has_expansion"] else None,
            "meaningfully_adds": False if payload[side]["has_expansion"] else None,
            "unnecessarily_verbose": False if payload[side]["has_expansion"] else None,
            "incorrect": False if payload[side]["has_expansion"] else None,
            "notes": "The supplied profile supports Austin.",
            "evidence_refs": ["evidence.digest", f"{side}.initial"],
            "uncertainty": None,
        }
    return result


def fixture_settings() -> SimpleNamespace:
    # Deliberately synthetic rate card, never a claim about production prices.
    return SimpleNamespace(
        model_prices={MODEL: ModelPrice(input=1, cached=0.1, output=5)},
        tier_price_multipliers=DEFAULT_TIERS,
        openai_base_url=None,
    )


def arguments(tmp_path: Path, *, count: int = 2, live: bool = True) -> Any:
    source = tmp_path / "review.json"
    source.write_text(json.dumps(sample_review(count)), encoding="utf-8")
    return judge.parser().parse_args(
        [
            "--review",
            str(source),
            "--out",
            str(tmp_path / "output"),
            "--judge",
            f"{MODEL}:low",
            "--max-cost",
            "none",
            "--reps",
            "2",
            *(["--live"] if live else []),
        ]
    )


class FakeProvider:
    name = "openai"

    def __init__(self, *, invalid: bool = False, known_usage: bool = True, delay: float = 0.01):
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.invalid = invalid
        self.known_usage = known_usage
        self.delay = delay
        self.payloads: list[dict[str, Any]] = []

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        assert params["reasoning"] == {"effort": "low"}
        assert isinstance(req.content, str)
        assert req.json_schema
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        payload = json.loads(req.content)
        self.payloads.append(payload)
        try:
            await asyncio.sleep(self.delay)
            yield "{invalid" if self.invalid else json.dumps(valid_response(payload))
            yield LLMDone(
                LLMUsage(
                    input_tokens=1000,
                    output_tokens=400,
                    cache_write_tokens=0 if self.known_usage else None,
                    reasoning_tokens=20,
                ),
                "end_turn",
                model=model,
                provider="openai",
            )
        finally:
            self.active -= 1


def test_payload_is_allowlisted_and_anonymous() -> None:
    item = sample_review()["items"][0]
    item["A"].update(model="SECRET model", latency_ms=123, notes="SECRET old assessment")
    item["A"]["delivery"].update(error="SECRET architecture failure")
    payload = judge.blind_payload(item, ["B", "A"])
    encoded = json.dumps(payload)
    assert "SECRET" not in encoded
    assert "private-pair" not in encoded
    assert "latency" not in encoded
    assert payload["evidence"] == {"digest": "Candidate lives in Austin."}
    assert payload["B"]["boundary_units"][1]["id"] == "deep:1"


def test_cue_and_complete_sentence_are_independent_and_optional() -> None:
    payload = judge.blind_payload(sample_review()["items"][0], ["A", "B"])
    response = valid_response(payload)
    validated = judge.validate_judgment(response, payload)
    assert validated["A"]["first_useful_cue"] == "fast:1"
    assert validated["A"]["first_useful_complete_sentence"] == "deep:1"
    assert validated["A"]["unit_grammar"] == {"fast:1": False, "deep:1": True}
    response["A"]["first_useful_complete_sentence"] = None
    assert judge.validate_judgment(response, payload)["A"]["first_useful_complete_sentence"] is None


@pytest.mark.parametrize(
    "fault", ["fragment", "unknown_id", "unassessed", "bad_score", "bad_ref", "extra"]
)
def test_invalid_judgments_are_rejected(fault: str) -> None:
    payload = judge.blind_payload(sample_review()["items"][0], ["A", "B"])
    response = valid_response(payload)
    if fault == "fragment":
        response["A"]["first_useful_complete_sentence"] = "fast:1"
    elif fault == "unknown_id":
        response["A"]["first_useful_cue"] = "deep:9"
    elif fault == "unassessed":
        response["A"]["first_useful_cue_assessed"] = False
    elif fault == "bad_score":
        response["A"]["scores"]["grounding"] = True
    elif fault == "bad_ref":
        response["evidence_refs"] = ["invented-source"]
    else:
        response["architecture"] = "unknown"
    with pytest.raises(ValueError, match=r"Invalid|Selection|Complete|Scores|Unknown"):
        judge.validate_judgment(response, payload)


def test_absent_and_nonexpanded_answers_are_not_forced_into_scores() -> None:
    item = sample_review()["items"][0]
    item["A"].update(initial="", expanded="", boundary_units=[], has_expansion=False)
    payload = judge.blind_payload(item, ["A", "B"])
    response = valid_response(payload)
    with pytest.raises(ValueError, match="Absent"):
        judge.validate_judgment(response, payload)
    response.update(preference="uncertain", uncertainty="One answer was not delivered.")
    assert judge.validate_judgment(response, payload)["A"]["scores"]["grounding"] is None
    response["A"]["redundant"] = False
    with pytest.raises(ValueError, match="No expansion"):
        judge.validate_judgment(response, payload)


def test_plan_is_finite_balanced_counterbalanced_and_reproducible() -> None:
    review = sample_review(5)
    judges = [judge.Judge(MODEL, "low"), judge.Judge("gpt-6.2-astra", "low")]
    jobs = judge.plan(review, judges, 2, 81)
    assert jobs == judge.plan(review, judges, 2, 81)
    assert len(jobs) == 20
    assert len({row["id"] for row in jobs}) == 20
    for setting in judges:
        for pair in review["items"]:
            matching = sorted(
                (
                    row
                    for row in jobs
                    if row["judge"] == setting.id and row["pair_id"] == pair["pair_id"]
                ),
                key=lambda row: row["rep"],
            )
            assert matching[0]["order"] == matching[1]["order"][::-1]
        first = [row["order"][0] for row in jobs if row["judge"] == setting.id and row["rep"] == 0]
        assert abs(first.count("A") - first.count("B")) <= 1


def test_mapping_and_export_preserve_original_review_content() -> None:
    source = sample_review(1)
    setting = judge.Judge(MODEL, "low")
    payload = judge.blind_payload(source["items"][0], ["B", "A"])
    response = valid_response(payload)
    response.update(preference="A", evidence_refs=["A.fast:1", "evidence.digest"])
    validated = judge.validate_judgment(response, payload)
    mapped = judge.map_back(validated, ["B", "A"])
    assert mapped["preference"] == "B"
    assert mapped["evidence_refs"][0] == "B.fast:1"
    job = judge.plan(source, [setting], 1, 1)[0]
    exported = judge.export_review(
        source, [job], {job["id"]: {**job, "status": "validated", "judgment": mapped}}, setting, 0
    )
    assert review_content(exported["items"][0]) == review_content(source["items"][0])
    assert exported["reviewer_kind"] == "ai"
    assert exported["human_review_outstanding"] is True


@pytest.mark.asyncio
async def test_live_mock_is_bounded_metered_and_resume_makes_no_calls(tmp_path: Path) -> None:
    args = arguments(tmp_path)
    provider = FakeProvider()
    summary = await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert summary["validated_jobs"] == 4
    assert summary["requests"] == 4
    assert summary["cost_usd"] == pytest.approx(0.012)
    assert provider.calls == 4
    assert provider.maximum_active == 2
    assert (args.out / "source-archive.zip").exists()
    assert await judge.execute(args, settings=fixture_settings(), provider=provider) == summary
    assert provider.calls == 4
    assert all("source" not in payload["evidence"] for payload in provider.payloads)


@pytest.mark.asyncio
async def test_dry_run_has_no_calls_and_changed_identity_refuses_resume(tmp_path: Path) -> None:
    args = arguments(tmp_path, live=False)
    provider = FakeProvider()
    assert (await judge.execute(args, settings=fixture_settings(), provider=provider))["jobs"] == 4
    assert provider.calls == 0
    assert not (args.out / "cost.json").exists()
    args.max_output_tokens += 1
    with pytest.raises(ValueError, match="Immutable"):
        await judge.execute(args, settings=fixture_settings(), provider=provider)


@pytest.mark.asyncio
async def test_retries_are_finite_and_invalid_billed_outputs_are_counted(tmp_path: Path) -> None:
    args = arguments(tmp_path, count=1)
    args.reps, args.retries = 1, 1
    provider = FakeProvider(invalid=True)
    summary = await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert summary["status"] == "partial"
    assert summary["failed_jobs"] == 1
    assert provider.calls == 2
    assert summary["requests"] == 2
    assert summary["cost_usd"] == pytest.approx(0.006)


@pytest.mark.asyncio
async def test_unknown_usage_never_becomes_zero_cost(tmp_path: Path) -> None:
    args = arguments(tmp_path, count=1)
    args.reps = 1
    summary = await judge.execute(
        args, settings=fixture_settings(), provider=FakeProvider(known_usage=False)
    )
    assert summary["cost_usd"] is None
    assert summary["known_cost_usd"] == 0
    assert summary["unpriced_or_unresolved_requests"] == 1
    assert summary["unresolved_reserved_usd"] > 0


@pytest.mark.asyncio
async def test_timeout_retains_reservation_and_terminal_checkpoint(tmp_path: Path) -> None:
    args = arguments(tmp_path, count=1)
    args.reps, args.timeout = 1, 0.005
    provider = FakeProvider(delay=0.05)
    summary = await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert summary["failed_jobs"] == 1
    assert summary["cost_usd"] is None
    assert provider.active == 0
    await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_started_request_is_never_redispatched_or_priced_zero(
    tmp_path: Path,
) -> None:
    args = arguments(tmp_path, count=1, live=False)
    args.reps = 1
    await judge.execute(args, settings=fixture_settings())
    job = json.loads((args.out / "plan.json").read_text("utf-8"))[0]
    folder = args.out / "jobs" / job["id"]
    folder.mkdir(parents=True)
    (folder / "attempt-0.started.json").write_text("{}", encoding="utf-8")
    args.live = True
    provider = FakeProvider()
    summary = await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert provider.calls == 0
    assert summary["failed_jobs"] == 1
    assert summary["cost_usd"] is None
    assert summary["cost_upper_usd"] is None
    assert summary["unmetered_interrupted_requests"] == 1


def test_lock_excludes_another_runner_and_releases(tmp_path: Path) -> None:
    with (
        judge.run_lock(tmp_path),
        pytest.raises(ValueError, match="Another runner"),
        judge.run_lock(tmp_path),
    ):
        pytest.fail("Second lock was acquired")
    with judge.run_lock(tmp_path):
        pass


def test_disagreements_include_assessed_none_and_position_reversal() -> None:
    review = sample_review(1)
    payload = judge.blind_payload(review["items"][0], ["A", "B"])
    first = judge.validate_judgment(valid_response(payload), payload)
    second = deepcopy(first)
    second["preference"] = "A"
    second["A"]["first_useful_complete_sentence"] = None
    results = {
        "one": {"id": "one", "pair_id": "private-pair-0", "status": "validated", "judgment": first},
        "two": {
            "id": "two",
            "pair_id": "private-pair-0",
            "status": "validated",
            "judgment": second,
        },
    }
    fields = {row["field"] for row in judge.disagreements(review, results)}
    assert {"pair.preference", "first_useful_complete_sentence"} <= fields


def test_budget_requires_explicit_finite_cap_or_none() -> None:
    assert judge.budget_limit("none") is None
    assert judge.budget_limit("1.25") == 1.25
    for value in ("inf", "nan", "0", "-1"):
        with pytest.raises(argparse.ArgumentTypeError, match="Budget"):
            judge.budget_limit(value)


def test_export_imports_shared_review_schema_with_synthetic_test_key() -> None:
    from copilot_evals.live_answer_eval import fingerprint, read_judgments

    source = sample_review(1)
    setting = judge.Judge(MODEL, "low")
    job = judge.plan(source, [setting], 1, 1)[0]
    response = judge.validate_judgment(
        valid_response(judge.blind_payload(source["items"][0], ["A", "B"])),
        judge.blind_payload(source["items"][0], ["A", "B"]),
    )
    exported = judge.export_review(
        source, [job], {job["id"]: {**job, "status": "validated", "judgment": response}}, setting, 0
    )
    # Fabricated test assignments only; the runner never reads a real identity key.
    key = {
        "pairs": {
            "private-pair-0": {
                "content_sha256": fingerprint(review_content(source["items"][0])),
                "assignment": {
                    "A": {"architecture": "fixture-a", "id": "a"},
                    "B": {"architecture": "fixture-b", "id": "b"},
                },
            }
        }
    }
    imported = read_judgments(exported, key)
    assert imported["fixture-a"]["a"]["first_useful_cue"] == "fast:1"
    assert imported["fixture-a"]["a"]["first_useful_complete_sentence"] == "deep:1"
    assert imported["fixture-a"]["a"]["first_useful_complete_sentence_assessed"] is True


@pytest.mark.asyncio
async def test_currency_cap_blocks_before_provider_dispatch(tmp_path: Path) -> None:
    args = arguments(tmp_path, count=1)
    args.reps = 1
    args.max_cost = 0.000001
    provider = FakeProvider()
    summary = await judge.execute(args, settings=fixture_settings(), provider=provider)
    assert summary["status"] == "partial"
    assert summary["requests"] == 0
    assert provider.calls == 0
    assert summary["cost_usd"] == 0


def test_missing_units_schema_never_contains_invalid_empty_enum() -> None:
    item = sample_review(1)["items"][0]
    item["A"].update(initial="", expanded="", boundary_units=[], has_expansion=False)
    schema = judge.output_schema(judge.blind_payload(item, ["A", "B"]))
    assert '"enum": []' not in json.dumps(schema)
    assert schema["properties"]["A"]["properties"]["unit_grammar"]["maxItems"] == 0
