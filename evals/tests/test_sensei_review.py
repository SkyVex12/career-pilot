"""Independent AI agreement must never masquerade as human validation."""

import pytest
from copilot_evals.sensei_judge import RUBRIC
from copilot_evals.sensei_report import calibration
from copilot_evals.sensei_review import validate_review
from copilot_evals.sensei_runtime import save


def valid_review():
    return {
        "A": {"scores": dict.fromkeys(RUBRIC, 4), "invented": 0},
        "B": {"scores": dict.fromkeys(RUBRIC, 3), "invented": 0},
        "preference": "A",
        "reason": "Synthetic directness difference",
    }


def test_ai_agreement_does_not_pass_human_trust_gate(tmp_path):
    reviews = [{"id": i, **valid_review()} for i in range(15)]
    summary = {
        "questions": [
            {
                "id": i,
                "preference": "ours",
                "ours": {"initial": [4] * 6},
                "sensei": {"initial": [3] * 6},
            }
            for i in range(15)
        ]
    }
    grades = [{"id": i, "round": 0, "order": ["ours", "sensei"]} for i in range(15)]
    save(
        tmp_path / "ai-review.json", {"reviewer_kind": "ai", "model": "synthetic", "items": reviews}
    )
    result = calibration(tmp_path, summary, grades, ai=True)
    assert result["status"] == "ai_agreement_only"
    assert result["meets_agreement_threshold"]
    assert result["preference_agreement"] == 1
    assert all(value == 0 for value in result["criterion_mae"].values())
    assert calibration(tmp_path, summary, grades)["completed"] == 0


def test_duplicate_review_cannot_inflate_sample(tmp_path):
    save(tmp_path / "human-review.json", [{"id": 1, **valid_review()}] * 15)
    summary = {
        "questions": [
            {
                "id": 1,
                "preference": "ours",
                "ours": {"initial": [4] * 6},
                "sensei": {"initial": [3] * 6},
            }
        ]
    }
    grades = [{"id": 1, "round": 0, "order": ["ours", "sensei"]}]
    with pytest.raises(ValueError, match="Duplicate"):
        calibration(tmp_path, summary, grades)


def test_review_requires_real_integer_scores_and_nonnegative_claims():
    result = valid_review()
    validate_review(result)
    result["A"]["scores"]["relevance"] = True
    with pytest.raises(ValueError, match="integer"):
        validate_review(result)
    result = valid_review()
    result["B"]["invented"] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        validate_review(result)
