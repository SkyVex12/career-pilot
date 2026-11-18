import json
from collections.abc import Mapping
from typing import Any

from copilot_core import screen
from copilot_core.llm import LLMRequest, openai_content
from copilot_core.prompts import CallContext

WEBP = "UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"


def shot(data: str = WEBP, media: str = "image/webp", title: str | None = None) -> screen.Shot:
    return screen.Shot(data=data, media_type=media, width=1, height=1, title=title)


def blocks(req: LLMRequest) -> list[Mapping[str, Any]]:
    assert not isinstance(req.content, str)
    return list(req.content)


def feed_all(chunks: list[str]) -> tuple[str, screen.ReadSplitter]:
    s = screen.ReadSplitter()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    return out, s


def test_image_bytes_must_match_the_media_type() -> None:
    assert screen.image_ok(shot(WEBP, "image/webp"))
    assert screen.image_ok(shot(PNG, "image/png"))
    assert screen.image_ok(shot(JPEG, "image/jpeg"))
    assert not screen.image_ok(shot(PNG, "image/webp"))
    assert not screen.image_ok(shot(WEBP, "image/jpeg"))
    assert not screen.image_ok(shot("not base64!!", "image/png"))
    assert not screen.image_ok(shot("aGVsbG8=", "image/png"))  # "hello"


def test_size_from_base64_length() -> None:
    assert shot(PNG).size_bytes == 68
    assert shot("aGVsbG8=").size_bytes == 5


def test_the_read_header_is_taken_out_of_the_stream_across_chunks() -> None:
    reading = {
        "tasks": [
            {"n": 1, "kind": "choice", "title": "Big-O"},
            {"n": 2, "kind": "code", "title": "Merge", "language": "python",
             "signature": "def merge(self, a):", "approach": "sort, then merge"},
        ],
        "language": "python",
    }  # fmt: skip
    text = "READ " + json.dumps(reading) + "\n### 1. Big-O\n**Answer:** B) O(log n)"
    chunks = [text[i : i + 7] for i in range(0, len(text), 7)]
    out, s = feed_all(chunks)
    assert out == "### 1. Big-O\n**Answer:** B) O(log n)"
    assert s.reading == reading
    assert screen.needs_code(s.reading)
    assert screen.summary(s.reading) == "Screen: 2 tasks (choice, code), python"
    assert screen.wire_reading(reading)["tasks"][1] == {
        "n": 2, "kind": "code", "title": "Merge", "language": "python"
    }  # fmt: skip


def test_nothing_shows_while_the_header_is_arriving() -> None:
    s = screen.ReadSplitter()
    assert s.feed('READ {"tasks": [{"n": 1, ') == ""
    assert s.feed('"kind": "short"}]}\nA hash map.') == "A hash map."
    assert s.reading == {"tasks": [{"n": 1, "kind": "short"}]}
    assert not screen.needs_code(s.reading)
    assert s.feed(" More.") == " More."


def test_the_text_after_the_header_starts_clean_at_any_chunk_boundary() -> None:
    header = 'READ {"tasks":[{"n":1,"kind":"choice","title":"x"}]}'
    text = header + "\n\n### 1. x\n**Answer:** B"
    for cut in range(len(header) - 3, len(header) + 4):
        out, _ = feed_all([text[:cut], text[cut:]])
        assert out == "### 1. x\n**Answer:** B", cut


def test_an_answer_without_the_header_flows_through_whole() -> None:
    out, s = feed_all(["**Answer:** C", ") 42"])
    assert out == "**Answer:** C) 42"
    assert s.reading is None
    assert screen.needs_code(None)  # unknown: the code pass covers it


def test_a_header_in_a_code_fence_or_that_never_closes() -> None:
    out, s = feed_all(['```\nREAD {"tasks": [{"kind": "sql"}]}\n```\nSELECT 1;'])
    assert s.reading == {"tasks": [{"n": 1, "kind": "sql"}]}
    assert out == "SELECT 1;"
    out, s = feed_all(['READ {"tasks": [', "oops"])
    assert s.reading is None
    assert out == 'READ {"tasks": [oops'


def test_unreadable_screens_and_unknown_kinds() -> None:
    _, s = feed_all(
        ['READ {"tasks": [], "unreadable": "a blank desktop"}\nPlease take another shot.']
    )
    assert s.reading == {"tasks": [], "unreadable": "a blank desktop"}
    assert not screen.needs_code(s.reading)
    assert screen.summary(s.reading) == "Screen: nothing readable"
    cleaned = screen.clean_reading({"tasks": [{"kind": "poem"}, {"kind": "fix", "title": " Bug "}]})
    assert cleaned == {"tasks": [{"n": 1, "kind": "fix", "title": "Bug"}]}
    assert screen.clean_reading(["not", "a", "reading"]) is None


def test_fast_pass_puts_each_shot_with_its_title_and_the_call_in_an_interview() -> None:
    call = CallContext(recent="Them: please use Java, and no extra memory")
    shots = [shot(title="Problem - Chrome"), shot(PNG, "image/png", title="Main.java - VS Code")]
    req = screen.fast_pass(
        "Ana", shots, answer_mode="interview", call=call, language=None,
        default_language="python", max_tokens=1000,
    )  # fmt: skip
    system = req.system[0].text
    assert "Ana" in system
    assert "{user_name}" not in system
    assert "live technical interview" in system
    assert "**Say first:**" in system
    assert "With no language anywhere, python." in system
    assert req.system[0].cache
    content = blocks(req)
    assert content[0] == {"type": "text", "text": "Shot 1 of 2, window: Problem - Chrome"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/webp", "data": WEBP}
    assert content[2]["text"] == "Shot 2 of 2, window: Main.java - VS Code"
    assert "no extra memory" in content[4]["text"]
    parts = openai_content(content)
    assert parts[1]["type"] == "input_image"
    assert parts[1]["detail"] == "high"


def test_assessment_has_no_call_and_a_pinned_language_wins() -> None:
    req = screen.fast_pass(
        "Ana", [shot()], answer_mode="assessment", call=CallContext(recent="Them: hi"),
        language="java", default_language="python", max_tokens=1000,
    )  # fmt: skip
    system = req.system[0].text
    assert "online coding assessment" in system
    assert "**Say first:**" not in system
    assert "Ana chose java: all code is in java" in system
    assert "<transcript>" not in blocks(req)[-1]["text"]


def test_code_pass_gets_the_reading() -> None:
    reading = {"tasks": [{"n": 1, "kind": "code", "approach": "two pointers"}]}
    req = screen.code_pass(
        "Ana", [shot()], reading, answer_mode="assessment", call=None, language=None,
        default_language="python", max_tokens=4000,
    )  # fmt: skip
    assert "ready to paste" in req.system[0].text
    tail = blocks(req)[-1]["text"]
    assert "<reading>" in tail
    assert "two pointers" in tail
    req = screen.code_pass(
        "Ana", [shot()], None, answer_mode="interview", call="Them: go ahead",
        language=None, default_language="python", max_tokens=4000,
    )  # fmt: skip
    tail = blocks(req)[-1]["text"]
    assert "read the tasks from the shots yourself" in tail
    assert "go ahead" in tail
