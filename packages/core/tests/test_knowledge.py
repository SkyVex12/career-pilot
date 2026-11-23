"""Engineering context without a stack: cutting a document into sections, keyword search
over them, the question's context, the company digest, and what the prompts say at work."""

import uuid
from datetime import date
from typing import Any

from copilot_core import knowledge, prompts
from copilot_core.knowledge import Index, LiveChunk

DOC = """Intro before any heading.

# Reports

## Engine

The engine runs a report: it reads the saved definition and asks the search index.

```mermaid
graph LR
  # a comment in the diagram

  api --> engine
```

## Limits

- An export has at most 100,000 rows.

### Retention

Entries are deleted after 360 days.
"""


def test_a_document_is_cut_at_its_headings_and_keeps_their_path() -> None:
    sections = knowledge.split(DOC)
    assert [s.path for s in sections] == [
        "",
        "Reports > Engine",
        "Reports > Limits",
        "Reports > Limits > Retention",
    ]
    assert [s.ord for s in sections] == [0, 1, 2, 3]
    # A fenced diagram is one piece of its section, blank lines and "#" lines included.
    assert "# a comment in the diagram\n\n  api --> engine" in sections[1].text
    assert sections[3].text == "Entries are deleted after 360 days."
    assert "".join(s.text for s in sections).count("100,000") == 1


def test_a_document_without_headings_and_a_long_section_are_cut_by_size() -> None:
    paragraphs = [" ".join([f"word{n}"] * 90) for n in range(7)]
    sections = knowledge.split("\n\n".join(paragraphs))
    assert len(sections) == 4  # two paragraphs each, then the last
    assert all(s.path == "" for s in sections)
    assert all(len(s.text.split()) <= knowledge.SECTION_WORDS for s in sections)
    table = "# Wide table\n" + "\n".join(f"| row {n} | {'x ' * 20}|" for n in range(60))
    cut = knowledge.split(table)
    assert len(cut) > 1
    assert {s.path for s in cut} == {"Wide table"}
    assert knowledge.split("   \n\n") == []


def chunk(path: str, text: str, doc: str = "design.md") -> LiveChunk:
    return LiveChunk(id=uuid.uuid4(), document_id=uuid.uuid4(), doc=doc, path=path, text=text)


CHUNKS = [
    chunk("Change log > The index", "It reads the activity-stream alias, one index for all."),
    chunk("Form logs > The index", "It reads the form-record index, one document per row."),
    chunk("Depreciation > Rules", "Expense is the sum of the monthly amounts in the range."),
    chunk("Libraries", "reports-document renders the file with jsPDF and ExcelJS."),
    chunk("Rollout", "Deploy the mapping first, then the lambdas, then the client."),
]


def test_keyword_search_finds_the_one_line_extraction_would_drop() -> None:
    index = Index.build(CHUNKS)
    [(top, _), *_] = index.search("so is that expense an average or a sum of those months", 3)
    assert top.path == "Depreciation > Rules"
    # A name counts whole and by its parts, and a plural finds its singular.
    assert index.search("which pdf library do the reports documents use", 1)[0][0].path == (
        "Libraries"
    )
    # Words every spoken question has match nothing.
    assert index.search("okay so what do you think about that then", 3) == []
    assert Index.build([]).search("anything", 3) == []
    assert index.get(CHUNKS[1].id) is CHUNKS[1]


def test_a_short_question_that_points_back_is_searched_with_the_calls_names() -> None:
    index = Index.build(CHUNKS)
    names = ["Change log", "Form logs", "reports-document"]
    question = "what's the name of the index?"
    # Alone it cannot tell the two indexes apart (2026-11-13: it got the other feature's).
    assert knowledge.needs_context(question, names)
    recent = "Them: tell me about form logs\nAlex: sure\nThem: and how is the change log built?"
    said = knowledge.context_names(recent, names)
    assert said == ["Change log", "Form logs"]  # the latest said first
    found = index.search(f"{question} {said[0]}", 1)
    assert found[0][0].path == "Change log > The index"
    # A question that names its subject, or is long enough, needs no second search.
    assert not knowledge.needs_context("how does reports-document render a file?", names)
    long = "can we get the first submission for each asset from the tables we have today"
    assert not knowledge.needs_context(long, names)
    assert knowledge.context_names("Them: the weather is nice", names) == []


def test_results_are_interleaved_and_cut_for_the_short_answer() -> None:
    a, b, c, d = CHUNKS[:4]
    assert knowledge.merge([a, b], [c, a], [d], limit=3) == [a, c, d]
    assert knowledge.merge([], [], limit=3) == []
    long = chunk("Flow", " ".join(f"filler{n}" for n in range(200)) + " the retry cap is five")
    cut = knowledge.excerpt(long.text, "what is the retry cap", 20)
    assert cut.startswith("... ")
    assert "the retry cap" in cut
    assert len(cut.split()) <= 22
    refs: dict[str, Any] = {}
    [line] = knowledge.memory_lines([long], refs, "what is the retry cap", 20)
    [sid] = refs
    assert sid.startswith("n:k")
    assert line.startswith(f"[{sid}] (doc: design.md > Flow) ... ")
    assert refs[sid].node_id == long.id
    assert refs[sid].label == "design.md > Flow"
    [whole] = knowledge.memory_lines([long], refs, "what is the retry cap", None)
    assert "filler0 filler1" in whole  # the detailed answer gets the section whole
    assert list(refs) == [sid]  # and the same id for the same section


def node(kind: str, title: str, summary: str, source: str = "company_document",
         day: date | None = None, **body: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_DNS, f"{kind}:{title}"),
        "kind": kind,
        "title": title,
        "summary": summary,
        "body": body,
        "source": {"type": source},
        "valid_from": day,
        "created_at": None,
    }


def test_the_company_digest_is_a_system_map_with_reasons_and_the_same_bytes_each_time() -> None:
    nodes = [
        node("system", "Export worker", "Renders one part of a large export",
             talks_to=["Export queue"], stack=["Lambda"]),
        node("decision", "Calculate inside the search cluster", "Run the calculation there",
             day=date(2026, 10, 1), why="Totals cover every record",
             alternatives=["A Lambda: must fetch every record"],
             tradeoff="Two engines to keep in step"),
        node("decision", "Older decision", "Something earlier", day=date(2026, 9, 1)),
        node("flow", "Large export", "How a big file is made",
             steps=["The API queues a job", "Workers render parts", "One merge writes the file"]),
        node("constraint", "Export cap", "At most 100,000 rows per export"),
        node("risk", "Two engines", "The two calculation engines can drift apart"),
        node("term", "Run log", "One record for every file a report produced"),
        node("system", "Mentioned once", "Named in a work log", source="worklog"),
        node("work_item", "A ticket", "Not part of a system map", source="worklog"),
    ]  # fmt: skip
    sids = {n["id"]: f"n:w{i:03x}" for i, n in enumerate(nodes)}
    digest = knowledge.render_digest("Fernhill", nodes, sids)
    assert digest.text == knowledge.render_digest("Fernhill", list(reversed(nodes)), sids).text
    lines = digest.text.splitlines()
    assert lines[1] == "## System map"
    # A document's component comes before a work log's passing mention.
    assert lines[2] == (
        "- [n:w000] Export worker: Renders one part of a large export. Talks to: Export"
        " queue. Stack: Lambda."
    )
    assert "- [n:w007] Mentioned once: Named in a work log." in lines
    newest, older = (i for i, x in enumerate(lines) if "] Calculate" in x or "] Older" in x)
    assert newest < older  # decisions newest first
    assert lines[newest] == (
        "- [n:w001] Calculate inside the search cluster: Run the calculation there. Why:"
        " Totals cover every record. Rejected: A Lambda: must fetch every record. Tradeoff:"
        " Two engines to keep in step."
    )
    assert (
        "- [n:w003] Large export: How a big file is made. 1) The API queues a job. 2) Workers"
        " render parts. 3) One merge writes the file."
    ) in lines
    for heading in ("## Constraints and limits", "## Open risks", "## Glossary"):
        assert heading in lines
    assert "A ticket" not in digest.text
    assert digest.refs["n:w001"].node_id == nodes[1]["id"]
    assert digest.names == ["Export worker", "Mentioned once", "Large export", "Run log"]
    # Work-log mentions alone are no system map: no digest, and the prompt stays as it was.
    assert knowledge.render_digest("Fernhill", nodes[-2:], sids).text == ""
    many = [node("term", f"Term {n}", "x" * 300) for n in range(80)] + nodes[:1]
    assert len(knowledge.render_digest("Fernhill", many, {}).text) <= knowledge.DIGEST_MAX_CHARS


def ctx(mode: str, digest: str | None = "## System map\n- [n:w001] Export worker") -> Any:
    return prompts.PromptContext(
        user_name="Alex Rivera",
        mode=mode,
        digest_text="career",
        pack_text="Mode: " + mode,
        company="Fernhill" if mode != "interview" else None,
        company_digest=digest,
    )


def test_at_work_the_digest_is_its_own_cached_block_and_a_gap_gets_the_safe_answer() -> None:
    req = prompts.fast_pass(ctx("standup"), "", "why did we pick that?", None, 200)
    tags = [b.text.split(">")[0] for b in req.system[1:]]
    assert tags == ["<digest", "<company_digest", "<session_pack"]
    assert all(b.cache for b in req.system[1:])
    rules = req.system[0].text
    assert "come only from <company_digest>, <session_pack> or <memory>" in rules
    assert "they will confirm the rest and follow up" in rules
    assert "never how Fernhill's does" in rules
    assert "is not a status question" in rules
    # Without a digest, and in an interview, the prompt has no such block.
    plain = prompts.fast_pass(ctx("standup", None), "", "q", None, 200)
    assert len(plain.system) == 3
    interview = prompts.fast_pass(ctx("interview"), "", "q", None, 200)
    assert len(interview.system) == 3
    assert "company_digest" not in interview.system[0].text


def test_work_questions_have_their_own_types_and_shapes() -> None:
    standup = prompts.detector("Alex Rivera", "", "was that added on purpose?", 20, "standup")
    schema = standup.json_schema or {}
    answers = schema["properties"]["answer"]["enum"]
    assert set(prompts.WORK_TYPES) <= set(answers)
    rule = standup.system[0].text
    assert "decision: why something was chosen" in rule
    assert "said as a statement and nothing is asked" in rule
    interview = prompts.detector("Alex Rivera", "", "why did you leave?", 20, "interview")
    assert not set(prompts.WORK_TYPES) & set(
        (interview.json_schema or {})["properties"]["answer"]["enum"]
    )
    assert "how_it_works" not in interview.system[0].text
    # Each goes out as a type every app knows.
    assert set(prompts.WIRE_TYPES) == set(prompts.WORK_TYPES)
    assert set(prompts.WIRE_TYPES.values()) <= set(prompts.QUESTION_TYPES)
    shapes = {
        "decision": "what was rejected and why not",
        "how_it_works": "three short steps in order",
        "status_follow_up": "That one item only",
        "estimate_risk": "A size or a date only if an entry has one",
    }
    for qtype, words in shapes.items():
        deep = prompts.deep_pass(ctx("standup"), "", "q", qtype, 800)
        assert f"Question ({qtype}): q\nShape: " in str(deep.content)
        assert words in str(deep.content)


def test_a_standup_answer_is_never_told_about_the_suggestion_marker() -> None:
    # 2026-11-13: the standup style named the marker, and the model put it on cited facts.
    for mode in ("standup", "meeting"):
        for style in ("bullets", "paragraph"):
            deep = prompts.deep_pass(ctx(mode), "", "q", "decision", 800, style=style)
            assert "[~]" not in str(deep.content).split("Style:")[1]
    interview = prompts.deep_pass(ctx("interview"), "", "q", "technical", 800)
    assert "[~]" in str(interview.content)


def test_work_meeting_can_request_daily_status_without_losing_technical_shapes() -> None:
    meeting = ctx("meeting")
    rules = prompts.fast_pass(meeting, "", "What are your plans?", "standup_status", 200)
    assert "use the standup section of <session_pack>" in rules.system[0].text
    assert "today's plan, blockers and commitments" in rules.system[0].text
    detector = prompts.detector("Alex Rivera", "", "What's your update?", 20, "meeting")
    assert "whole update or plan in either work mode" in detector.system[0].text
    assert "standup_status in a standup, else respond" not in detector.system[0].text
    daily = prompts.deep_pass(meeting, "", "What's your update?", "standup_status", 800)
    assert "From the standup section only" in str(daily.content)
    technical = prompts.deep_pass(meeting, "", "Why did we choose SQS?", "decision", 800)
    assert "what was rejected and why not" in str(technical.content)
