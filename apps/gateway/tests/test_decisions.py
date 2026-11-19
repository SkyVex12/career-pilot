"""The detector on OpenAI's Decisions API (2026-11-16): the request, reading its answers, the
provider call against a recorded response, the hedge, and a whole session deciding with it.
In the speed baseline (docs/analysis/2026-11-14-speed-baseline.md) 27 of 35 questions had
their answer ready and waited for the detector; the same rules as typed choices decided in
about 0.5 s instead of 1.2 s."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import openai

from copilot_core import keys, prompts
from copilot_core.llm import Decided, LLMUsage, OpenAIProvider
from copilot_gateway.detector import DecisionsRoute, Detector, detection_from
from copilot_gateway.router import build_router, decisions_route
from copilot_gateway.testing import GatewayServer, ProtocolClient, ScriptedProvider

from .test_openai import OPENAI_SETTINGS, openai_settings

FIXTURE = Path(__file__).parent / "fixtures" / "openai_decision.json"


def answers(
    turn: str, qtype: str, confidence: float = 0.9, depth: str | None = None
) -> list[dict[str, Any]]:
    out = [
        {"type": "choice", "name": "turn", "choice": turn, "confidence": confidence},
        {"type": "choice", "name": "type", "choice": qtype, "confidence": 0.6},
    ]
    if depth is not None:
        out.append({"type": "choice", "name": "depth", "choice": depth, "confidence": 0.8})
    return out


def test_the_decisions_request_carries_the_detector_rules_and_two_questions() -> None:
    req = prompts.detector_decision(
        "Alex Rivera", "[00:10] Them: Thanks.", "Where are you based?", "interview"
    )
    rules = req.input[0]["content"][0]["text"]
    user = req.input[1]["content"][0]["text"]
    assert req.input[0]["role"] == "developer"
    assert req.input[1]["role"] == "user"
    # The detector's own rules, without its JSON output line, plus what the API got wrong.
    assert "will you need sponsorship" in rules
    assert "Return JSON" not in rules
    assert "An open question about Alex Rivera's own situation" in rules
    assert "<utterance>\nWhere are you based?\n</utterance>" in user
    turn, kind, depth = req.questions
    assert (turn["name"], kind["name"], depth["name"]) == ("turn", "type", "depth")
    assert [c["value"] for c in depth["choices"]] == ["deep", "light"]
    assert [c["value"] for c in turn["choices"]] == ["answer", "wait", "none"]
    interview = {c["value"] for c in kind["choices"]}
    assert interview == {
        "behavioral", "technical", "system_design", "coding", "small_talk", "other", "respond",
    }  # fmt: skip
    # A standup offers the status and work types, and tells the turn question about checks
    # and problems said as statements.
    standup = prompts.detector_decision("Alex Rivera", "", "Is that merged?", "standup", "t")
    types = {c["value"] for c in standup.questions[1]["choices"]}
    assert {"standup_status", *prompts.WORK_TYPES} <= types
    assert "even when said as a statement" in standup.questions[0]["instructions"]
    assert "<turn_taking>" in standup.input[1]["content"][0]["text"]


def test_a_decisions_answer_becomes_the_detectors_decision() -> None:
    recorded = json.loads(FIXTURE.read_text("utf-8"))["answers"]
    detection, confidence = detection_from(recorded, "interview")
    assert (detection.decision, detection.qtype) == ("answer", "technical")
    assert confidence > 0.9
    assert detection_from(answers("wait", "other"), "interview")[0].decision == "wait"
    assert not detection_from(answers("none", "technical"), "interview")[0].fires()
    # A refusal, or a work type outside a call at work, never fires as something odd.
    refused = [{"type": "refusal", "name": "turn"}, {"type": "refusal", "name": "type"}]
    assert not detection_from(refused, "interview")[0].fires()
    assert detection_from(answers("answer", "decision"), "interview")[0].qtype == "other"
    assert detection_from(answers("answer", "decision"), "standup")[0].qtype == "decision"


async def test_the_openai_provider_decides_through_the_decisions_endpoint() -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=json.loads(FIXTURE.read_text("utf-8")))

    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    client = openai.AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)
    req = prompts.detector_decision("Alex", "", "How did you scale the ledger?", "interview")
    got = await OpenAIProvider("openai", client).decide("gpt-6-luna", req.input, req.questions)
    [request] = sent
    assert request.url.path == "/v1/decisions"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6-luna"
    assert [q["name"] for q in body["questions"]] == ["turn", "type", "depth"]
    assert got.answers[0]["choice"] == "answer"
    # Only input is billed: the recorded call read 1,327 tokens and wrote none.
    assert got.usage == LLMUsage(input_tokens=1327, cached_tokens=0, output_tokens=0)


class FakeDecider:
    name = "openai"

    def __init__(self, delays: Sequence[float], errors: Sequence[bool] = ()) -> None:
        self.delays = list(delays)
        self.errors = list(errors)
        self.calls = 0

    async def decide(
        self,
        model: str,
        input: Sequence[Mapping[str, Any]],
        questions: Sequence[Mapping[str, Any]],
    ) -> Decided:
        i = self.calls
        self.calls += 1
        await asyncio.sleep(self.delays[i])
        if i < len(self.errors) and self.errors[i]:
            raise RuntimeError("Decision inference is unavailable.")
        return Decided(answers("answer", "technical"), LLMUsage(1300), "openai", model)


def detector_on(decider: FakeDecider, hedge_s: float | None = 0.2) -> tuple[Detector, list[Any]]:
    entries: list[Any] = []
    route = DecisionsRoute(decider, "gpt-6-luna", hedge_s, timeout_s=2.0)
    router = build_router(openai_settings(), providers=[])
    det = Detector(router, "Alex", 80, on_decision=lambda e, _u: entries.append(e), decisions=route)
    return det, entries


async def test_a_slow_decision_is_hedged_and_a_failed_one_retried() -> None:
    # The first request stalls: a second starts at the hedge time and its answer is used.
    slow = FakeDecider([1.5, 0.05])
    det, entries = detector_on(slow)
    got = await det.detect("How did you scale it?", "", "interview")
    assert got.fires()
    assert got.latency_ms < 600
    assert slow.calls == 2
    assert entries[-1]["hedged"] is True
    assert entries[-1]["api"] == "decisions"
    # A request that fails (the API answered 503 now and then) is retried at once.
    flaky = FakeDecider([0.01, 0.05], errors=[True])
    det, entries = detector_on(flaky, hedge_s=None)
    got = await det.detect("How did you scale it?", "", "interview")
    assert got.fires()
    assert flaky.calls == 2
    # Two failures count as no question, logged with the error.
    down = FakeDecider([0.01, 0.01], errors=[True, True])
    det, entries = detector_on(down, hedge_s=None)
    got = await det.detect("How did you scale it?", "", "interview")
    assert not got.fires()
    assert "unavailable" in (got.error or "")
    assert entries[-1]["error"]


def test_the_decisions_route_needs_an_openai_provider_and_the_setting() -> None:
    class Openai(ScriptedProvider):
        decide = FakeDecider([0.0]).decide

    openai_router = build_router(openai_settings(), providers=[Openai("openai")])
    assert decisions_route(openai_settings(), openai_router) is not None
    off = openai_settings(detector_api="responses")
    assert decisions_route(off, openai_router) is None
    claude = build_router(
        openai_settings(llm_providers=["anthropic"]), [ScriptedProvider("anthropic")]
    )
    assert decisions_route(openai_settings(), claude) is None


class OpenAIScripted(ScriptedProvider):
    """The scripted provider under OpenAI's name, deciding on the Decisions API: a "?" at the
    end of the utterance is a technical question."""

    def __init__(self) -> None:
        super().__init__("openai")
        self.decisions: list[str] = []

    async def decide(
        self,
        model: str,
        input: Sequence[Mapping[str, Any]],
        questions: Sequence[Mapping[str, Any]],
    ) -> Decided:
        text = str(input[1]["content"][0]["text"])
        utterance = text.split("<utterance>")[-1].split("</utterance>")[0].strip()
        self.decisions.append(utterance)
        turn = "answer" if utterance.endswith("?") else "none"
        return Decided(answers(turn, "technical"), LLMUsage(1300), "openai", model)


async def test_a_session_decides_on_the_decisions_api_alone(env) -> None:
    provider = OpenAIScripted()
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(**OPENAI_SETTINGS))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?")
        q = await c.wait_type("question.detected", speculative=False)
        assert q["question_type"] == "technical"
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.say("We run the payments platform.")
        await c.wait_type("transcript.final")
        await asyncio.sleep(0.5)
        await c.send({"type": "session.end"})
        await c.closed()
    assert provider.decisions == ["How did you scale the ledger?", "We run the payments platform."]
    # The Responses API is never asked to detect: only the fast pass ran there.
    assert "detector" not in [job for job, _ in provider.calls]
    assert len([m for m in c.of_type("question.detected") if not m["speculative"]]) == 1
    log = [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(c.session_id))]
    decided = [e for e in log if e["kind"] == "detector" and not e.get("cached")]
    assert {e["api"] for e in decided} == {"decisions"}
    assert all(e["output_tokens"] == 0 for e in decided)
    assert all(e["cost_usd"] is not None for e in decided)
    assert decided[0]["confidence"] == 0.9


def test_the_depth_question_says_which_model_writes_the_detailed_answer() -> None:
    """2026-11-16: gpt-6-luna's detailed answer was done in 3.0 s against 8.0 s on gpt-6.1-sol,
    as complete on stories and less so on technical depth, design and the job questions. The
    deep model writes only what needs expert reasoning."""
    from copilot_gateway.answer import AnswerEngine, Question

    assert detection_from(answers("answer", "technical", depth="light"), "interview")[0].depth == (
        "light"
    )
    assert detection_from(answers("answer", "technical"), "interview")[0].depth is None

    class Host:
        def __init__(self, routing: str) -> None:
            self.settings = openai_settings(deep_routing=routing)

    def job(routing: str, qtype: str, depth: str | None) -> str:
        q = Question(
            qid=uuid4(), text="q", qtype=qtype, confidence=0.9, addressed=True,
            speculative=False, forced=False, t0=0.0, detector_ms=None, match=None, depth=depth,
        )  # fmt: skip
        return AnswerEngine(Host(routing)).deep_job(q)  # pyright: ignore[reportArgumentType]

    assert job("by_need", "behavioral", "deep") == "deep"
    assert job("by_need", "technical", "light") == "deep_light"
    # No word from the detector: the type decides.
    assert job("by_need", "technical", None) == "deep"
    assert job("by_need", "other", None) == "deep"
    assert job("by_need", "behavioral", None) == "deep_light"
    assert job("by_need", "standup_status", None) == "deep_light"
    assert job("always_deep", "behavioral", "light") == "deep"


def test_the_light_detailed_answer_runs_on_the_fast_model() -> None:
    from copilot_gateway.router import build_routes

    routes = build_routes(openai_settings(), [ScriptedProvider("openai")])
    assert [r.model for r in routes["deep_light"]] == [r.model for r in routes["fast"]]
    assert routes["deep_light"][0].params == routes["fast"][0].params
    claude = build_routes(openai_settings(), [ScriptedProvider("anthropic")])
    assert "thinking" not in claude["deep_light"][0].params
    assert claude["deep_light"][0].model == claude["fast"][0].model


async def test_a_session_routes_the_detailed_answer_by_its_depth(env) -> None:
    class Routed(OpenAIScripted):
        async def decide(self, model, input, questions):  # type: ignore[no-untyped-def]
            text = str(input[1]["content"][0]["text"])
            depth = "deep" if "design" in text.split("<utterance>")[-1] else "light"
            return Decided(
                answers("answer", "technical", depth=depth), LLMUsage(1300), "openai", model
            )

    provider = Routed()
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(**OPENAI_SETTINGS))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        for text in ("How would you design a rate limiter?", "How did your day go so far?"):
            await c.say(text)
            q = await c.wait_type("question.detected", speculative=False, text=text)
            await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        await c.send({"type": "session.end"})
        await c.closed()
    deep_models = [model for job, model in provider.calls if job == "deep"]
    assert deep_models == ["gpt-6.1-sol", "gpt-6-luna"]


async def test_a_session_warms_the_light_detailed_answers_cache_too(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await asyncio.sleep(0.3)
        await c.send({"type": "session.end"})
        await c.closed()
    # The fast pass's prefix, then the detailed answer's on the same cheap model.
    assert len(provider.prewarms) == 2
    assert all(model == provider.prewarms[0][0] for model, _ in provider.prewarms)
    assert "talking" not in provider.prewarms[1][1].system[0].text
