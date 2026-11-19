"""Synthetic reporting checks for addressee adjudication and absolute quality risks."""

import json
from copy import deepcopy

import pytest
from copilot_evals.sensei_report import compare, eligibility_scope, report
from copilot_evals.sensei_runtime import save


def write_run(directory):
    rows = []
    scores = []
    for identifier, our_score, their_score, answered in (
        (1, 4, 3, True),
        (2, 1, 5, False),
        (3, 1, 3, False),
    ):
        rows.append(
            {
                "id": identifier,
                "question": f"Synthetic prompt {identifier}",
                "suggestion": "Synthetic competitor cue",
                "paraphrase": f"Rewritten intent {identifier}",
                "type": "technical",
                "needs_answer": True,
                "fast": "Synthetic delivered cue" if answered else "",
                "deep": "",
                "detected": answered,
                "cancelled": False,
                "cards": [f"card-{identifier}"] if answered else [],
                "offset_s": identifier * 10,
                "latency": {"fast_first_s": 1.0} if answered else {},
            }
        )
        scores.append(
            {
                "id": identifier,
                "type": "technical",
                "needs_answer": True,
                "ours": {
                    "initial": [our_score] * 6,
                    "expanded": [our_score] * 6,
                    "invented_initial": [1, 1] if answered else [0, 0],
                    "invented_expanded": [2, 2] if answered else [0, 0],
                },
                "sensei": {
                    "initial": [their_score] * 6,
                    "expanded": [their_score] * 6,
                    "invented_initial": [0, 0],
                    "invented_expanded": [0, 0],
                },
                "preference": "ours" if answered else "sensei",
                "expanded_preference": "ours" if answered else "sensei",
                "score_disagreement": False,
            }
        )
    summary = {"judge_model": "synthetic", "questions": scores}
    save(directory / "summary.json", summary)
    save(directory / "answers.json", rows)
    save(directory / "grades.json", [])
    save(directory / "cost.json", [])
    save(directory / "replay.json", {"speed": 1.0})
    save(
        directory / "diagnosis.json",
        {
            "losses": [
                {
                    "id": identifier,
                    "category": "not detected" if identifier != 1 else "prompt/shape",
                    "reason": f"Synthetic diagnosis {identifier}",
                    "evidence_level": "observed",
                    "effort": "small",
                    "experiment": (
                        "Excluded participant recommendation"
                        if identifier == 2
                        else f"Candidate experiment {identifier}"
                    ),
                }
                for identifier in (1, 2, 3)
            ],
            "strengths": [{"id": 1, "reason": "Preserve directness"}],
        },
    )
    save(
        directory / "eligibility.json",
        {
            "model": "independent-synthetic",
            "items": [
                {"id": 1, "addressee": "candidate", "reason": "Private audit reasoning"},
                {"id": 2, "addressee": "other", "reason": "Private audit reasoning"},
                {"id": 3, "addressee": "unclear", "reason": "Private audit reasoning"},
            ],
        },
    )
    return rows, summary


def test_report_excludes_only_other_and_preserves_original_sensitivity(tmp_path):
    write_run(tmp_path)
    report(tmp_path, tmp_path / "report.md", tmp_path / "fixtures.json")
    text = (tmp_path / "report.md").read_text("utf-8")
    assert "primary candidate-question scope contains 2 turns" in text
    assert "Excluded turn IDs: [2]" in text
    assert "| Primary candidate scope | 2 | 1 | 1 | 2.50 | 2.50 | 3.00 | 1 / 0 / 1 / 0 |" in text
    assert (
        "| Original response-seeking labels | 3 | 1 | 1 | 2.00 | 2.00 | 3.67 | 1 / 0 / 2 / 0 |"
        in text
    )
    assert "Initial answers: 1/2" in text
    assert "Excluded participant recommendation" not in text
    assert "Candidate experiment 3" in text
    fixture_text = (tmp_path / "fixtures.json").read_text("utf-8")
    fixture = json.loads(fixture_text)
    assert fixture["primary_question_ids"] == [1, 3]
    assert fixture["turns"][1]["needs_answer"]
    assert not fixture["turns"][1]["in_primary_scope"]
    assert fixture["turns"][2]["in_primary_scope"]
    assert "Private audit reasoning" not in fixture_text
    public_scores = json.loads((tmp_path / "fixtures.scores.json").read_text("utf-8"))
    assert len(public_scores["questions"]) == 3
    assert public_scores["excluded_question_ids"] == [2]


def test_report_exposes_absolute_risk_without_negative_gain(tmp_path):
    write_run(tmp_path)
    report(tmp_path, tmp_path / "report.md", tmp_path / "fixtures.json")
    text = (tmp_path / "report.md").read_text("utf-8")
    assert "No delivered answer has a positive aggregate initial-score deficit" in text
    assert "| 1 | Rewritten intent 1 | 4.00 / 4.00 | [1, 1] | [2, 2] |" in text
    assert "Candidate experiment 1" in text
    assert "+0.125 / small (absolute quality; headroom to 5)" in text
    assert "+-" not in text


def test_eligibility_rejects_incomplete_or_duplicated_audits(tmp_path):
    rows, _ = write_run(tmp_path)
    audit = json.loads((tmp_path / "eligibility.json").read_text("utf-8"))
    audit["items"] = [audit["items"][0], audit["items"][0], audit["items"][2]]
    save(tmp_path / "eligibility.json", audit)
    with pytest.raises(ValueError, match="exactly once"):
        eligibility_scope(tmp_path, rows)
    audit["items"] = audit["items"][:1]
    save(tmp_path / "eligibility.json", audit)
    with pytest.raises(ValueError, match="exactly once"):
        eligibility_scope(tmp_path, rows)


def test_before_after_uses_identical_candidate_scope(tmp_path):
    baseline, current = tmp_path / "before", tmp_path / "after"
    baseline.mkdir()
    current.mkdir()
    _, summary = write_run(baseline)
    write_run(current)
    for directory in (baseline, current):
        for name in ("inputs.json", "profile.json", "annotations.json"):
            save(directory / name, {})
        save(
            directory / "judge-identity.json",
            {"model": "synthetic", "rubric_sha256": "a", "evidence_sha256": "b", "seed": 1},
        )
    changed = deepcopy(summary)
    changed["questions"][0]["ours"]["initial"] = [5] * 6
    changed["questions"][1]["ours"]["initial"] = [5] * 6
    save(current / "summary.json", changed)
    compare(baseline, current, tmp_path / "comparison.md")
    text = (tmp_path / "comparison.md").read_text("utf-8")
    assert "| relevance | 2.50 | 3.00 | +0.50 |" in text
    audit = json.loads((current / "eligibility.json").read_text("utf-8"))
    audit["items"][1]["addressee"] = "candidate"
    save(current / "eligibility.json", audit)
    with pytest.raises(ValueError, match="eligibility differs"):
        compare(baseline, current, tmp_path / "comparison.md")


def test_comparison_keeps_baseline_delivered_cohort_when_outputs_change(tmp_path):
    baseline, current = tmp_path / "before", tmp_path / "after"
    baseline.mkdir()
    current.mkdir()
    write_run(baseline)
    rows, summary = write_run(current)
    for directory in (baseline, current):
        for name in ("inputs.json", "profile.json", "annotations.json"):
            save(directory / name, {})
        save(
            directory / "judge-identity.json",
            {
                "model": "synthetic",
                "rubric_sha256": "a",
                "evidence_sha256": "b",
                "seed": 1,
            },
        )
    rows[0].update(fast="", detected=False, cancelled=True, cards=["withdrawn"])
    summary["questions"][0]["ours"].update(initial=[1] * 6, expanded=[1] * 6)
    for index in (1, 2):
        rows[index].update(
            fast="New synthetic cue",
            deep="More synthetic detail",
            detected=True,
            cards=["new", "extra"],
        )
        summary["questions"][index]["ours"].update(initial=[5] * 6, expanded=[5] * 6)
    save(current / "answers.json", rows)
    save(current / "summary.json", summary)
    output = tmp_path / "comparison.md"
    compare(baseline, current, output)
    text = output.read_text("utf-8")
    assert "Fixed primary scope: 2 turns" in text
    assert "| Nonempty initial-output turns | 1/2 | 1/2 |" in text
    assert "| Nonempty deep-output turns | 0/2 | 1/2 |" in text
    assert "| Surviving visible-card turns | 1/2 | 1/2 |" in text
    assert "| Recorded card IDs, including withdrawn | 1 | 3 |" in text
    assert "| Turns with multiple recorded card IDs | 0 | 1 |" in text
    assert "| Overall | 2.50 | 3.00 | +0.50 | +0.50 |" in text
    cohort = text.split("## Baseline-delivered initial-answer cohort (n=1)")[1]
    assert "| Overall | 4.00 | 1.00 | -3.00 | -3.00 |" in cohort
    assert "newly recovered questions do not enter this cohort" in cohort
    assert "Human calibration is not established for both runs" in text
    assert "New synthetic cue" not in text
    assert "More synthetic detail" not in text


def test_report_uses_run_scoped_costs_and_does_not_assert_unchanged_production(tmp_path):
    write_run(tmp_path)
    save(
        tmp_path / "cost.json",
        [
            {"model": "synthetic", "charged_usd": 0.25, "status": "usage"},
            {"model": "synthetic", "charged_usd": 0.75, "status": "reserved"},
            {"model": "synthetic", "charged_usd": 0.0, "status": "failed_before_submission"},
        ],
    )
    save(
        tmp_path / "ai-review.json",
        {
            "reviewer_kind": "ai",
            "model": "synthetic",
            "items": [],
        },
    )
    report(tmp_path, tmp_path / "report.md", tmp_path / "fixtures.json")
    text = (tmp_path / "report.md").read_text("utf-8")
    assert "Usage-accounted calls: $0.250000" in text
    assert "reservations: $0.750000" in text
    assert "Combined conservative ledger: $1.000000" in text
    assert "selected run directory's ledger only" in text
    assert "not confirmed billed charges" in text
    assert "Reusing evidence or labels does not copy the costs" in text
    assert "AI-only agreement; human calibration remains unvalidated" in text
    assert "No production prompts or answer code changed" not in text
    assert "No changes from this plan have been made" not in text
