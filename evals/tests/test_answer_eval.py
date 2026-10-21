"""The answer-quality eval's offline parts: the question set and the counts with no model."""

from copilot_evals.answer_eval import items, text_metrics


def test_items_are_the_benchmark_questions_that_need_an_answer() -> None:
    got = {i.id: i for i in items()}
    assert not {"share_screen", "greet", "unfinished"} & got.keys()
    assert got["rate_limiter"].qtype == "system_design"
    assert "that has to work across several regions?" in got["rate_limiter"].question
    # The transcript before a question holds the earlier lines, as the live call would.
    assert "Them: Tell me about a time" in got["incident_followup"].transcript


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
