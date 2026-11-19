"""Synthetic records only; no material copied from a private export."""

import json

import pytest
from copilot_evals.sensei import inspect_export, main, metadata, parse_turns

SYNTHETIC = (
    "#1\nNov 01, 1:00:00 PM\nHow would you troubleshoot a slow chart?\n"
    "Measure query time and browser rendering separately.\n"
    "#2\nNov 01, 1:00:07 PM\nWhich measurement comes first?\n"
    "Start with an end-to-end trace.\n"
)


def test_requires_confirmation_and_preserves_timing():
    records = inspect_export(SYNTHETIC, year=2026)
    with pytest.raises(ValueError, match="Confirm field roles"):
        parse_turns(records, confirmed_question_then_suggestion=False)
    turns = parse_turns(records, confirmed_question_then_suggestion=True)
    assert [t.offset_s for t in turns] == [0, 7]
    assert turns[0].question == records[0].first_text
    assert turns[0].suggestion == records[0].second_text
    assert all(t.speaker == "interviewer" for t in turns)
    assert records[0].first_text not in repr(records)
    assert records[0].second_text not in repr(turns)


@pytest.mark.parametrize(
    "text",
    [
        "",
        SYNTHETIC + "extra private content",
        SYNTHETIC.replace("#2", "#1"),
        SYNTHETIC.replace("#2", "private identifier"),
        SYNTHETIC.replace("1:00:07 PM", "private timestamp"),
        SYNTHETIC.replace("1:00:07 PM", "12:00:00 PM"),
        SYNTHETIC.replace("Which measurement comes first?", ""),
    ],
)
def test_rejects_bad_layout_without_echoing_content(text):
    with pytest.raises(ValueError, match=r"Expected|Record") as error:
        inspect_export(text, year=2026)
    assert "private" not in str(error.value)
    assert "slow chart" not in str(error.value)


def test_cli_prints_metadata_only(tmp_path, capsys):
    source = tmp_path / "synthetic.txt"
    source.write_text(SYNTHETIC, encoding="utf-8")
    assert main([str(source), "--year", "2026"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == metadata(inspect_export(SYNTHETIC, year=2026))
    assert "slow chart" not in output
    assert "trace" not in output
    assert str(source) not in output


def test_bom_crlf_and_duplicate_times():
    text = "\ufeff" + SYNTHETIC.replace("\n", "\r\n").replace("1:00:07", "1:00:00")
    assert metadata(inspect_export(text, year=2026))["duplicate_timestamps"] == 1
