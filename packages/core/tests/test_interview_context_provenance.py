"""Portable interview evidence-boundary regression; prompt builders only."""

import pytest

from copilot_core import prompts


@pytest.mark.parametrize("expert", [False, True])
@pytest.mark.parametrize("language", ["en", "es"])
@pytest.mark.parametrize("length", ["default", "concise", "detailed"])
def test_interview_draft_keeps_user_speech_separate_from_generated_suggestions(
    expert: bool, language: str, length: str
) -> None:
    context = prompts.PromptContext(
        user_name="Alex",
        mode="interview",
        spoken_language=language,
        digest_text="[n:1] Alex maintained Cedar's Redis cache.",
        pack_text="[r:2] Role: staff engineer responsible for reliable systems.",
    )
    call = prompts.CallContext(
        recent="Them: What changed next? Alex: I reviewed the cache behavior.",
        summary="Alex described cache maintenance at Cedar.",
        answers="Q: What did you do? Alex: I reviewed cache eviction settings.",
        suggestions="I led a Quartz rollout that cut costs by 71 percent.",
    )
    memory = ["[n:3] Cedar's review remained open; no rollout was recorded."]
    question = "Explain the cache work and why it fits this role."
    request = prompts.adaptive_pass(
        context,
        call,
        question,
        "behavioral",
        1200,
        memory,
        depth="narrative",
        expert=expert,
    )
    request = prompts.with_response_length(request, length, interview_drafting=True)
    assert isinstance(request.content, str)
    for tag, text in (
        ("memory", memory[0]),
        ("call_summary", call.summary),
        ("earlier_answers", call.answers),
        ("earlier_suggestions", call.suggestions),
        ("transcript", call.recent),
    ):
        assert f"<{tag}>\n{text}\n</{tag}>" in request.content
    assert f"Question (behavioral): {question}" in request.content
    assert request.content.count(call.suggestions) == 1
    assert all(call.suggestions not in block.text for block in request.system)
    assert context.digest_text is not None
    assert context.digest_text in request.system[1].text
    assert context.pack_text is not None
    assert context.pack_text in request.system[2].text
    rules = " ".join(" ".join(block.text for block in request.system).split())
    assert (
        "without [~], check labels, private coaching or disclaimers about missing evidence"
    ) in rules
    assert "Never invent evidence or experience." not in rules
    assert "or invent personal experience, metrics or unsupported facts" not in rules
    assert "Preserve supplied facts and follow the interview drafting policy." in rules
    assert "Personal particulars and commitments are not drafting details" in rules
    assert (
        "<earlier_suggestions> contains generated UI text, not user speech or evidence; "
        "never adopt its claims as facts."
    ) in rules
    assert request.max_tokens == 1200
    if expert:
        assert (
            "independent expert response using the original question and evidence"
        ) in request.content
        assert "Do not assume another generated answer is correct" in request.content
