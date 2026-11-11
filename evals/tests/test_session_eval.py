"""The session eval's fixtures and scoring, and its offline replay against a real gateway."""

import re

from copilot_evals import session_eval as se


def test_fixtures_load_and_their_labels_are_consistent() -> None:
    fixtures = se.load_fixtures()
    assert {f.id for f in fixtures} >= {"interview_screen", "standup_review", "standup_handoff"}
    for fx in fixtures:
        assert fx.mode in ("interview", "standup", "meeting")
        for line in fx.lines:
            if line.speaker == "me":
                assert line.expect == "either", (fx.id, line.index)
                assert not line.unfinished, (fx.id, line.index)
                assert line.follow in (None, "card", "none"), (fx.id, line.index)
            else:
                assert line.addressee in ("me", "other", "nobody"), (fx.id, line.index)
                assert line.follow is None, (fx.id, line.index)
                assert not line.update, (fx.id, line.index)
                if line.addressee != "me":
                    assert line.expect == "none", (fx.id, line.index)
            if line.check is not None:
                for pattern in (*line.check.must, *line.check.must_not):
                    re.compile(pattern)
        if fx.mode == "standup":
            assert fx.items, fx.id
            assert fx.company, fx.id


def test_a_question_carries_the_unfinished_lines_before_it() -> None:
    fx = next(f for f in se.load_fixtures() if f.id == "interview_screen")
    line = next(ln for ln in fx.lines if ln.whole == ("databases", "sql"))
    text = se.question_text(fx, line)
    assert text.startswith("And then what about databases")
    assert text.endswith("experienced with?")
    # The first half is the question, not the transcript before it.
    assert "databases" not in se.transcript_before(fx, line, "Alex Rivera")


def test_flow_scoring_counts_each_kind_of_miss() -> None:
    fx = next(f for f in se.load_fixtures() if f.id == "interview_screen")
    question = next(ln for ln in fx.lines if ln.whole == ("mind", "background"))
    quiet = next(ln for ln in fx.lines if ln.expect == "none" and not ln.unfinished)
    answered = next(ln for ln in fx.lines if ln.follow == "none")
    cards = [
        se.Card(question.index, "behavioral", "about your background, for me?"),  # a half
        se.Card(quiet.index, "other", quiet.text),  # a card where none is due
        se.Card(answered.index, "follow", "a second card on an answered turn"),
    ]
    scored = se.score_flow(fx, cards)
    assert scored["whole_question"][0] == 0
    assert scored["quiet"][0] == scored["quiet"][1] - 1
    assert scored["follow"][0] == scored["follow"][1] - 2  # that card, and the missing one
    assert len(scored["failed"]) >= 4


def test_a_check_is_one_point_per_pattern() -> None:
    check = se.Check(must=("(?i)export",), must_not=("(?i)uncommitted", "(?i)64 files"))
    rows = se.apply_check(check, "- Export left; work uncommitted")
    assert [ok for _, ok in rows] == [True, False, True]


async def test_the_replay_shows_one_card_per_labelled_question() -> None:
    # Offline: scripted speech to text and models; the detector answers from the labels.
    fx = next(f for f in se.load_fixtures() if f.id == "standup_handoff")
    scored = se.score_flow(fx, await se.replay(fx, "Alex Rivera"))
    assert scored["question_one_card"] == [2, 2]
    assert scored["quiet"][0] == scored["quiet"][1]
