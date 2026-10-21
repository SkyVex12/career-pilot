"""The answer-quality eval's offline parts: the question set, the counts with no model, and the
summary."""

from typing import Any

from copilot_evals.answer_eval import grader_input, items, known_ids, summarize, text_metrics


def test_items_are_the_benchmark_questions_that_need_an_answer() -> None:
    got = {i.id: i for i in items()}
    assert not {"share_screen", "greet", "unfinished"} & got.keys()
    assert got["rate_limiter"].qtype == "system_design"
    assert "that has to work across several regions?" in got["rate_limiter"].question
    # The transcript before a question holds the earlier lines, as the live call would.
    assert "Them: Tell me about a time" in got["incident_followup"].transcript
    assert got["incident"].covered
    assert not got["process_thread"].covered


def test_items_add_questions_the_profile_does_not_cover() -> None:
    got = {i.id: i for i in items()}
    new = [i for i in got.values() if i.key_points]
    types = {i.qtype for i in new}
    assert {"technical", "system_design", "behavioral", "other", "coding"} <= types
    assert not any(i.covered for i in new)
    assert all(i.trap and "Them: Hi Alex" in i.transcript for i in new)
    assert {"salary", "why_company", "learn_fast", "ledger"} <= got.keys()


def test_grader_sees_key_points_and_the_trap() -> None:
    salary = next(i for i in items() if i.id == "salary")
    text = grader_input("# digest", "Mode: interview", salary, "deep", "- ask their range")
    assert "<key_points>\n- no figure stated" in text
    # The grader sees what the interviewer said: facts from it are not invented.
    assert "<transcript>\n[00:01] Them: Hi Alex" in text
    assert "Check for invented facts: the profile has no current salary" in text
    assert text.endswith("<answer>\n- ask their range\n</answer>")


def test_text_metrics_tell_cues_from_written_sentences() -> None:
    cues = text_metrics(
        "- Black Friday outage [n:s1]\n"
        "- pool ran dry at 6x traffic\n"
        "- flag, load shed in 12 min [n:s1]"
    )
    assert (cues["lines"], cues["sentence_lines"], cues["stock_phrases"]) == (3, 0, [])
    assert cues["words_per_line"] == 5.0
    assert cues["cited_share"] == 0.67
    prose = text_metrics(
        "- I leveraged a robust feature flag to ensure that checkout stayed up during the peak."
        " [n:s1]"
    )
    assert prose["sentence_lines"] == 1
    assert prose["stock_phrases"] == ["leveraged", "robust", "ensure"]


def test_text_metrics_count_citations_the_profile_does_not_have() -> None:
    known = known_ids()
    assert "n:s1" in known
    got = text_metrics("- outage [n:s1]\n- k8s probes at Larkspur [n:z9]", known)
    assert got["unknown_citations"] == ["n:z9"]


def test_summary_reports_uncovered_questions_apart_and_the_cost() -> None:
    def row(id_: str, covered: bool, complete: int) -> dict[str, Any]:
        grade = {
            "grounded": True, "invented": [], "complete": complete, "correct": 5,
            "spoken": 5, "shape": 5,
        }  # fmt: skip
        return {
            "id": id_, "pass": "deep", "covered": covered, "grade": grade, "cost_usd": 0.01,
            "metrics": text_metrics("- a cue here"),
        }  # fmt: skip

    s = summarize([row("incident", True, 5), row("salary", False, 2)])
    assert s["deep"]["complete_mean"] == 3.5
    assert s["uncovered"]["deep"]["complete_mean"] == 2
    assert s["uncovered"]["deep"]["answers"] == 1
    assert s["cost_usd"] == 0.02
