from __future__ import annotations

import copy

import pytest
from copilot_evals import live_answer_eval as base_eval
from copilot_evals import live_answer_judge as base_judge
from copilot_evals import live_answer_replay as base_replay
from copilot_evals import live_answer_review as base_review


def item():
    answer = {
        "initial": "A complete answer.",
        "expanded": "A complete answer.",
        "has_expansion": False,
        "boundary_units": [{"id": "fast:0", "pass": "fast", "text": "A complete answer."}],
        "delivery": {},
        "scores": {},
        "response_preferences": {"answer_style": "paragraph", "response_length": "concise"},
    }
    return {
        "pair_id": "p",
        "question": "Question?",
        "category": "technical",
        "evidence": {
            "digest": "Fictional source",
            "pack_text": "A source pack",
            "earlier_answers": "Me: Source speech.",
        },
        "preference": None,
        "A": copy.deepcopy(answer),
        "B": {
            **copy.deepcopy(answer),
            "response_preferences": {"answer_style": "bullets", "response_length": "detailed"},
        },
    }


def test_old_review_content_hash_unchanged_when_absent():
    x = item()
    for side in ("A", "B"):
        x[side].pop("response_preferences")
    # Frozen 33b9e0f content identity for this preference-free review fixture.
    assert (
        base_eval.fingerprint(base_eval.review_content(x))
        == "82922f92e73d8e0c2f1f137044e447c30668bbbde8ad4b27a3a2367f213e2a7c"
    )
    assert "response_preferences" not in base_judge.blind_payload(x, ["B", "A"])["A"]


@pytest.mark.parametrize(
    ("field", "value"), [("answer_style", "bullets"), ("response_length", "detailed")]
)
def test_each_explicit_preference_is_bound_to_review_hash(field, value):
    x = item()
    before = base_eval.fingerprint(base_eval.review_content(x))
    x["A"]["response_preferences"][field] = value
    assert before != base_eval.fingerprint(base_eval.review_content(x))


def test_both_preferences_follow_reversal_and_full_context_survives():
    x = item()
    payload = base_judge.blind_payload(x, ["B", "A"])
    assert payload["A"]["response_preferences"] == x["B"]["response_preferences"]
    assert payload["B"]["response_preferences"] == x["A"]["response_preferences"]
    assert payload["evidence"]["pack_text"] == "A source pack"
    assert payload["evidence"]["earlier_answers"] == "Me: Source speech."
    payload["A"]["response_preferences"]["answer_style"] = "paragraph"
    assert x["B"]["response_preferences"]["answer_style"] == "bullets"


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"answer_style": "paragraph"},
        {"answer_style": "paragraph", "response_length": "detailed", "prompt": "leak"},
        {"answer_style": "paragraphs", "response_length": "default"},
        {"answer_style": "bullets", "response_length": "huge"},
    ],
)
def test_preferences_reject_unrecognized_or_incomplete_values(value):
    x = item()
    x["A"]["response_preferences"] = value
    with pytest.raises(
        ValueError, match=r"Response preferences require|Unsupported recorded response preference"
    ):
        base_judge.blind_payload(x, ["A", "B"])


def test_blind_pairs_propagate_each_answer_own_requirement():
    runs = []
    for label, preferences in [
        ("X", item()["A"]["response_preferences"]),
        ("Y", item()["B"]["response_preferences"]),
    ]:
        row = {
            "id": "q",
            "question": "Question?",
            "type": "technical",
            "needs_answer": True,
            "fast": "A complete answer.",
            "deep": "",
            "evidence": {"digest": "Source"},
            "response_preferences": preferences,
        }
        runs.append(
            base_eval.Run(
                label, [row], {}, {"code": "same"}, {"source": "same"}, {"source": "same"}, []
            )
        )
    review, key = base_eval.blind_pairs(runs, 1, 71)
    pair = review["items"][0]
    for side in ("A", "B"):
        label = (
            key["pairs"][pair["pair_id"]]["assignment"][side]["architecture"]
            if "pairs" in key
            else key["items"][pair["pair_id"]]["assignment"][side]["architecture"]
        )
        assert (
            pair[side]["response_preferences"]
            == next(r for r in runs if r.architecture == label).rows[0]["response_preferences"]
        )


def test_human_review_displays_recorded_preference():
    html = base_review.review_html({"items": [item()]})
    assert "Requested style:" in html
    assert "response_preferences.answer_style" in html
    assert "response_preferences.response_length" in html


@pytest.mark.parametrize("missing", ["answer_style", "response_length"])
def test_partial_recorded_preferences_are_rejected_without_filling_defaults(missing):
    case = {
        "id": "q",
        "question": "Question?",
        "type": "technical",
        "mode": "interview",
        "digest": "Source",
        "recent": "",
        "memory": [],
        "source": "authored",
        "answer_style": "paragraph",
        "response_length": "concise",
    }
    case.pop(missing)
    with pytest.raises(ValueError, match="both"):
        base_replay.validate_cases([case])
    with pytest.raises(ValueError, match="both"):
        base_replay.normalized_runs([], [case], {"strategies": {"D": "adaptive_on_demand"}}, [])
