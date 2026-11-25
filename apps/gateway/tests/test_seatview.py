"""The guests' allow-list and view fold (seatview.py), over every server message the protocol
has: a type reaches a guest only if it is listed on purpose."""

import json
from pathlib import Path
from typing import Any

from copilot_gateway.seatview import FORWARDED, ViewFold, guest_message

FIXTURES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "packages/protocol/fixtures/messages.json").read_text(
        "utf-8"
    )
)
SCHEMA: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "packages/protocol/protocol.schema.json").read_text(
        "utf-8"
    )
)


def server_types() -> set[str]:
    defs = SCHEMA["$defs"]
    return {
        defs[r["$ref"].rsplit("/", 1)[1]]["properties"]["type"]["const"]
        for r in defs["ServerMessage"]["oneOf"]
    }


def test_only_listed_types_reach_a_guest() -> None:
    assert server_types() >= FORWARDED
    for message in FIXTURES["server"]:
        out = guest_message(json.dumps(message))
        assert (out is not None) == (message["type"] in FORWARDED), message["type"]
    # Never: the host's session, token, errors, seats, notes and sharing.
    assert not FORWARDED & {"session.ready", "auth.expiring", "error", "seats.update",
                            "seat.note", "sharing"}  # fmt: skip


def test_answer_done_loses_usage_source_and_timings() -> None:
    done = next(m for m in FIXTURES["server"] if m["type"] == "answer.done" and "latency_ms" in m)
    out = json.loads(guest_message(json.dumps(done)) or "{}")
    assert "latency_ms" not in out
    assert "source" not in out
    assert out["usage"] == {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    assert out["memory_refs"] == done["memory_refs"]


def m(seq: int, kind: str, **fields: Any) -> str:
    return json.dumps({"type": kind, "seq": seq, **fields})


def test_the_fold_keeps_finished_lines_merged_answers_and_honours_pauses() -> None:
    fold = ViewFold()
    q = "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210"
    fold.add(1, m(1, "session.ready"))
    fold.add(2, m(2, "transcript.partial", channel="them", seg_id=1, text="How did"))
    fold.add(3, m(3, "transcript.final", channel="them", seg_id=1, text="How did you?",
                  start_ms=0, end_ms=900))  # fmt: skip
    fold.add(4, m(4, "question.detected", question_id=q, text="How did you?",
                  question_type="other", confidence=0.9, speculative=True))  # fmt: skip
    fold.add(5, m(5, "answer.delta", question_id=q, **{"pass": "fast"}, text="- One"))
    fold.add(6, m(6, "answer.delta", question_id=q, **{"pass": "fast"}, text="\n- Two"))
    fold.add(7, m(7, "sharing", on=False))
    fold.add(8, m(8, "transcript.final", channel="them", seg_id=2, text="private",
                  start_ms=1000, end_ms=1500))  # fmt: skip
    fold.add(9, m(9, "sharing", on=True))
    fold.add(9, m(9, "transcript.final", channel="them", seg_id=3, text="dup", start_ms=0,
                  end_ms=1))  # an old seq is ignored  # fmt: skip
    kinds = [x["type"] for x in fold.messages()]
    assert kinds == ["transcript.final", "question.detected", "answer.delta"]
    assert fold.messages()[2]["text"] == "- One\n- Two"
    assert fold.seq == 9
    assert fold.sharing is True
    again = ViewFold.load(fold.dump())
    assert again is not None
    assert again.messages() == fold.messages()
    assert again.seq == 9


def test_counted_answer_ranges_and_strategy_survive_guest_snapshots() -> None:
    fold = ViewFold()
    fold.add(1, m(1, "question.detected", question_id="q", answer_strategy="adaptive_on_demand"))
    fold.add(2, m(2, "answer.delta", question_id="q", **{"pass": "fast"}, text="One ", sequence=0))
    fold.add(
        3, m(3, "answer.delta", question_id="q", **{"pass": "fast"}, text="sentence.", sequence=1)
    )
    fold.add(4, m(4, "answer.done", question_id="q", **{"pass": "fast"}, delta_count=2))
    messages = fold.messages()
    assert messages[0]["answer_strategy"] == "adaptive_on_demand"
    assert messages[1]["text"] == "One sentence."
    assert messages[1]["sequence"] == 0
    assert messages[1]["sequence_end"] == 2
    assert messages[2]["delta_count"] == 2
    restored = ViewFold.load(fold.dump())
    assert restored is not None
    assert restored.messages() == messages
    assert (
        json.loads(
            guest_message(m(4, "answer.done", question_id="q", **{"pass": "fast"}, delta_count=2))
            or "{}"
        )["delta_count"]
        == 2
    )


def test_guest_compaction_keeps_disjoint_and_post_terminal_chunks_separate() -> None:
    fold = ViewFold()
    for seq, index, text in ((1, 0, "A"), (2, 2, "C"), (3, 1, "B"), (4, 1, "duplicate")):
        fold.add(
            seq,
            m(seq, "answer.delta", question_id="q", **{"pass": "fast"}, text=text, sequence=index),
        )
    fold.add(5, m(5, "answer.done", question_id="q", **{"pass": "fast"}, delta_count=3))
    fold.add(6, m(6, "answer.delta", question_id="q", **{"pass": "fast"}, text="late", sequence=3))
    messages = fold.messages()
    assert [message.get("text") for message in messages] == [
        "A",
        "C",
        "B",
        "duplicate",
        None,
        "late",
    ]
    restored = ViewFold.load(fold.dump())
    assert restored is not None
    assert restored.messages() == messages
