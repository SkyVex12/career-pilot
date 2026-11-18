"""Coding mode through a real server with scripted models: shots in, the screen's reading as a
question, the fast pass's answers with its header taken out, the code pass started from the
reading, limits and bad images, consent. No network, no stack."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from copilot_core import keys
from copilot_core.ids import uuid7
from copilot_core.llm import LLMRequest
from copilot_gateway.testing import (
    SCREEN_READING,
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)

WEBP = "UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="


def image(data: str = WEBP, media: str = "image/webp", title: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"data": data, "media_type": media, "width": 1568, "height": 882}
    if title:
        out["title"] = title
    return out


async def session_log(env: Any, sid: str) -> list[dict[str, Any]]:
    raw = await env.client().xrange(keys.sess_log(sid))
    return [json.loads(f[b"e"]) for _, f in raw]


def requests_for(provider: ScriptedProvider, job: str) -> list[LLMRequest]:
    return [req for j, req in provider.requests if j == job]


def blocks(req: LLMRequest) -> list[Mapping[str, Any]]:
    assert not isinstance(req.content, str)
    return list(req.content)


def text_of(c: ProtocolClient, qid: str, pass_: str) -> str:
    return "".join(
        m["text"]
        for m in c.messages
        if m["type"] == "answer.delta" and m["question_id"] == qid and m["pass"] == pass_
    )


async def test_a_screen_is_read_answered_and_its_code_written(env) -> None:
    provider = env.providers[0]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Please solve it in Python, and keep memory constant.")
        await c.wait_type("transcript.final")
        qid = str(uuid7())
        shots = [image(title="Practice - Chrome"), image(PNG, "image/png", title="main.py")]
        await c.send({"type": "screenshot", "question_id": qid, "images": shots})
        first = await c.wait_type("question.detected", question_id=qid)
        assert first["question_type"] == "coding"
        assert "screen" not in first
        read = await c.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] == qid and "screen" in m
        )
        assert read["screen"]["tasks"][1] == {
            "n": 2, "kind": "code", "title": "Two sum", "language": "python"
        }  # fmt: skip
        assert read["text"] == "Screen: 2 tasks (choice, code), python"
        await c.wait_type("answer.done", question_id=qid, pass_="fast")
        done = await c.wait_type("answer.done", question_id=qid, pass_="deep")
        assert done["memory_refs"] == []
        await c.send({"type": "session.end"})
        await c.closed()
    fast = text_of(c, qid, "fast")
    assert fast.startswith("### 1. Binary search\n**Answer:** B) O(log n)")
    assert "READ" not in fast
    # The code reaches the client whole: no citation filter on `nums[n:]`.
    assert "return nums[n:]" in text_of(c, qid, "deep")
    [fast_req] = requests_for(provider, "screen_fast")
    content = blocks(fast_req)
    assert content[0]["text"] == "Shot 1 of 2, window: Practice - Chrome"
    assert content[3]["source"]["media_type"] == "image/png"
    assert "keep memory constant" in content[-1]["text"]  # the interviewer's words, in an interview
    [code_req] = requests_for(provider, "screen_code")
    assert "one pass with a hash map" in blocks(code_req)[-1]["text"]
    # The code is the deep model's, the reading the fast model's.
    models_by_job = dict(provider.calls)
    routes = gw.services.router.routes
    assert models_by_job["screen_code"] == routes["screen_code"][0].model
    assert models_by_job["screen_fast"] == routes["screen"][0].model
    assert routes["screen_code"][0].model == routes["deep"][0].model
    assert routes["screen"][0].model == routes["fast"][0].model
    log = await session_log(env, c.session_id)
    [shot_entry] = [e for e in log if e["kind"] == "screen"]
    assert shot_entry["answer_mode"] == "interview"
    assert [i["title"] for i in shot_entry["images"]] == ["Practice - Chrome", "main.py"]
    assert shot_entry["images"][0]["bytes"] == 34
    assert "data" not in json.dumps(shot_entry)
    [read_entry] = [e for e in log if e["kind"] == "screen_read"]
    assert read_entry["kinds"] == ["choice", "code"]
    assert read_entry["reading"] == SCREEN_READING
    assert [e["type"] for e in log if e["kind"] == "question"] == ["coding"]


async def test_an_assessment_has_no_call_and_a_screen_without_code_gets_no_code_pass(env) -> None:
    reading = {"tasks": [{"n": 1, "kind": "choice", "title": "Hash maps"}]}

    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "screen_fast":
            return Reply(["READ " + json.dumps(reading) + "\n**Answer:** A) O(1)\n"])
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(consent={"store_transcript": False})
        await c.say("Hello there, can you hear me?")
        await c.wait_type("transcript.final")
        await c.send(
            {
                "type": "screenshot",
                "images": [image(title="Quiz")],
                "answer_mode": "assessment",
                "language": "go",
            }
        )
        q = await c.wait_type("question.detected", question_type="coding")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await asyncio.sleep(0.2)
        await c.send({"type": "session.end"})
        await c.closed()
    assert text_of(c, q["question_id"], "fast") == "**Answer:** A) O(1)\n"
    assert not requests_for(provider, "screen_code")
    assert not any(m["type"] == "answer.delta" and m["pass"] == "deep" for m in c.messages)
    [req] = requests_for(provider, "screen_fast")
    assert "<transcript>" not in blocks(req)[-1]["text"]
    assert "all code is in go" in req.system[0].text
    log = await session_log(env, c.session_id)
    [shot_entry] = [e for e in log if e["kind"] == "screen"]
    assert "title" not in shot_entry["images"][0]  # no consent: no titles
    [read_entry] = [e for e in log if e["kind"] == "screen_read"]
    assert "reading" not in read_entry
    assert read_entry["kinds"] == ["choice"]


async def test_without_a_reading_the_code_pass_reads_the_screen_itself(env) -> None:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "screen_fast":
            return Reply(["Approach: sort then sweep."])
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "screenshot", "images": [image()]})
        q = await c.wait_type("question.detected", question_type="coding")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        await c.send({"type": "session.end"})
        await c.closed()
    assert text_of(c, q["question_id"], "fast") == "Approach: sort then sweep."
    [code_req] = requests_for(provider, "screen_code")
    assert "read the tasks from the shots yourself" in blocks(code_req)[-1]["text"]


async def test_a_new_screen_replaces_the_answer_before(env) -> None:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "screen_code":
            return Reply(["```python\n", "pass\n"] * 50, chunk_delay_s=0.05)
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "screenshot", "images": [image()]})
        q1 = await c.wait_type("question.detected", question_type="coding")
        await c.wait(lambda m: m["type"] == "answer.delta" and m["pass"] == "deep")
        await asyncio.sleep(1.1)
        await c.send({"type": "screenshot", "images": [image()]})
        await c.wait_type("answer.error", question_id=q1["question_id"], code="cancelled")
        await c.send({"type": "session.end"})
        await c.closed()


async def test_bad_and_too_many_screens(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "screenshot", "images": [image(PNG, "image/webp")]})
        bad = await c.wait_type("error", code="bad_message")
        assert bad["message"] == "image 1 is not a valid image/webp"
        assert bad["fatal"] is False
        await c.send({"type": "screenshot", "images": [image()] * 7})
        await c.wait(lambda m: m["type"] == "error" and m["code"] == "bad_message", start=3)
        await c.send({"type": "screenshot", "image": WEBP})  # the old shape
        n = len(c.messages)
        await c.wait(lambda m: m["type"] == "error" and m["code"] == "bad_message", start=n - 1)
        await c.send({"type": "screenshot", "images": [image()]})
        q = await c.wait_type("question.detected", question_type="coding")
        await c.send({"type": "screenshot", "images": [image()]})
        limited = await c.wait_type("error", code="rate_limited")
        assert 0 < limited["retry_after_ms"] <= 1000
        await asyncio.sleep(1.1)
        await c.send({"type": "screenshot", "question_id": q["question_id"], "images": [image()]})
        dup = await c.wait(
            lambda m: m["type"] == "error" and "already exists" in m.get("message", "")
        )
        assert dup["fatal"] is False
        # Six large shots fit in one message (6 x 500,000 base64 characters, under 4 MB).
        big = "UklGRhoAAABXRUJQ" + "A" * (500_000 - 16)
        await c.send({"type": "screenshot", "images": [image(big)] * 6})
        await c.wait(
            lambda m: (
                m["type"] == "question.detected"
                and m["question_type"] == "coding"
                and m["question_id"] != q["question_id"]
            )
        )
        await c.send({"type": "session.end"})
        await c.closed()
