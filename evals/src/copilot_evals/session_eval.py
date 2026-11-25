"""Whole calls, paraphrased from real sessions, scored on what went wrong in them.

    uv run copilot-session-eval run --label baseline            # all three parts
    uv run copilot-session-eval run --label try --part flow     # offline, no model calls
    uv run copilot-session-eval compare baseline after

The fixtures (evals/sessions/*.toml) are scripts with labelled times: who spoke when, whether
a card should show, for whom the turn was, its type, and what the answer must and must not
say. They come from the 2026-11-11 analysis of every session; no line is a real person's.

Three parts, each a score:

- flow (offline): the script is replayed through a real gateway, with speech to text and
  the models scripted. The detector answers from the labels, so this part measures the
  gateway's own turn handling given a perfect detector: one card per question, none for the
  rest, the whole question on the card, follow cards only where nothing answered and the
  user is answering, and the update card in a standup.
- detect (live): every labelled line of the other side goes to the configured detector with
  the transcript before it, as the gateway sends it. Fired against missed, false fires by
  addressee, type accuracy.
- answers (live): the lines with a `check` get a short answer (and with --deep the detailed
  one) from the gateway's own prompt builders and routes; each pattern is a check.

Live parts cost about $0.0001 a detector call and $0.0003 a short answer on gpt-6-luna; a
detailed answer on gpt-6.1-sol about $0.006. Each run adds its cost up.
"""

import argparse
import asyncio
import json
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from copilot_evals.answer_eval import add_cost, call_cost, format_cost, summarize_costs

SESSIONS = Path(__file__).resolve().parents[2] / "sessions"
RESULTS = SESSIONS / "results"
EVAL_USER = UUID("0192f3a9-55c2-7b10-a4e1-5e55105e5a11")
PARTS = ("flow", "detect", "answers")


@dataclass(frozen=True)
class Check:
    must: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()


@dataclass(frozen=True)
class Line:
    index: int
    t: float
    speaker: str  # "them" or "me"
    text: str
    expect: str = "either"  # them: answer, none, either
    addressee: str = "me"  # them: me, other, nobody
    qtype: str | None = None
    unfinished: bool = False
    whole: tuple[str, ...] = ()
    follow: str | None = None  # me: card, none
    update: bool = False
    check: Check | None = None
    case: str = ""

    @property
    def seconds(self) -> float:
        return 0.4 * len(self.text.split())  # about 150 words a minute


@dataclass(frozen=True)
class Fixture:
    id: str
    mode: str
    lines: list[Line]
    company: str | None = None
    role: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    source: str = ""


def load_fixture(path: Path) -> Fixture:
    data = tomllib.loads(path.read_text("utf-8"))
    meta = cast(dict[str, Any], data["session"])
    lines: list[Line] = []
    for i, raw in enumerate(cast(list[dict[str, Any]], data["line"])):
        chk = cast(dict[str, Any] | None, raw.get("check"))
        speaker = str(raw["speaker"])
        if speaker not in ("them", "me"):
            raise ValueError(f"{path.name} line {i}: speaker must be them or me")
        expect = str(raw.get("expect", "either"))
        if expect not in ("answer", "none", "either"):
            raise ValueError(f"{path.name} line {i}: expect {expect!r}")
        lines.append(
            Line(
                index=i,
                t=float(raw["t"]),
                speaker=speaker,
                text=str(raw["text"]),
                expect=expect,
                addressee=str(raw.get("addressee", "me")),
                qtype=raw.get("type"),
                unfinished=bool(raw.get("unfinished", False)),
                whole=tuple(str(w).lower() for w in raw.get("whole", ())),
                follow=raw.get("follow"),
                update=bool(raw.get("update", False)),
                check=Check(tuple(chk.get("must", ())), tuple(chk.get("must_not", ())))
                if chk
                else None,
                case=str(raw.get("case", "")),
            )
        )
    times = [ln.t for ln in lines]
    if times != sorted(times):
        raise ValueError(f"{path.name}: lines must be in order of time")
    return Fixture(
        id=str(meta["id"]),
        mode=str(meta["mode"]),
        lines=lines,
        company=meta.get("company"),
        role=meta.get("role"),
        items=list(cast(list[dict[str, Any]], data.get("item", []))),
        source=str(meta.get("source", "")),
    )


def load_fixtures(only: list[str] | None = None) -> list[Fixture]:
    out = [load_fixture(p) for p in sorted(SESSIONS.glob("*.toml"))]
    return [f for f in out if not only or f.id in only]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def question_text(fx: Fixture, line: Line) -> str:
    """The line with the unfinished lines of the same speaker right before it: the question
    as it was asked."""
    parts = [line.text]
    i = line.index - 1
    while i >= 0 and fx.lines[i].speaker == "them" and fx.lines[i].unfinished:
        parts.insert(0, fx.lines[i].text)
        i -= 1
    return " ".join(parts)


def stamp(seconds: float) -> str:
    return f"[{int(seconds) // 60:02d}:{int(seconds) % 60:02d}]"


def transcript_before(fx: Fixture, line: Line, user_name: str, window_s: float = 60.0) -> str:
    out: list[str] = []
    for ln in fx.lines[: line.index]:
        if ln.t + ln.seconds < line.t - window_s:
            continue
        if ln.speaker == "them" and ln.unfinished and line.index - ln.index == 1:
            continue  # part of the question itself
        who = "Them" if ln.speaker == "them" else user_name
        out.append(f"{stamp(ln.t)} {who}: {ln.text}")
    return "\n".join(out)


def standup_pack(fx: Fixture, today: date | None = None) -> Any:
    """The fixture's company memory rendered as the standup pack the gateway would load."""
    from copilot_core import workplace
    from copilot_core.packs import EmployerRef

    today = today or datetime.now(UTC).date()
    employer = EmployerRef(
        id=uuid5(EVAL_USER, fx.company or "company"), name=fx.company or "", title=fx.role or ""
    )
    nodes: list[dict[str, Any]] = []
    for item in fx.items:
        body = {"state": item.get("state", "done")}
        if item.get("external_id"):
            body["external_id"] = item["external_id"]
        nodes.append(
            {
                "id": uuid5(EVAL_USER, f"{fx.id}:{item['title']}"),
                "kind": "work_item",
                "title": item["title"],
                "summary": item["summary"],
                "body": body,
                "valid_from": today - timedelta(days=int(item.get("day", 1))),
            }
        )
    return workplace.standup_pack(EVAL_USER, employer, nodes, today, "eval")


# Flow: the gateway's turn handling, offline ---------------------------------------------------


WITHDRAWN = "withdrawn"  # a card shown and then taken back


@dataclass
class Card:
    line: int
    qtype: str
    text: str


def oracle(fx: Fixture) -> Any:
    """Model replies for the replay: the detector answers from the labels (the type for a
    line labelled "answer", "wait" for an unfinished one, "none" otherwise); the passes give
    fixed text."""
    from copilot_gateway.testing import Reply, default_reply

    labelled = [(norm(ln.text), ln) for ln in fx.lines if ln.speaker == "them"]

    def reply(job: str, req: Any) -> Any:
        if job != "detector":
            return default_reply(job, req)
        content = str(req.content)
        utterance = norm(content.split("<utterance>")[-1].split("</utterance>")[0])
        # The utterance may open with earlier words of the same turn: its last line decides.
        ends = [ln for text, ln in labelled if utterance.endswith(text)]
        line = max(ends, key=lambda ln: len(ln.text)) if ends else None
        if line is None:
            answer = "none"
        elif line.unfinished:
            answer = "wait"
        elif line.expect == "answer":
            answer = line.qtype or "other"
        else:
            answer = "none"
        return Reply([json.dumps({"answer": answer})])

    return reply


async def replay(fx: Fixture, user_name: str) -> list[Card]:
    """Replay the script through an in-process gateway; the cards it showed, by line."""
    from fakeredis import FakeAsyncValkey, FakeServer

    from copilot_core import keys
    from copilot_core.jobs import MemoryJobQueue
    from copilot_gateway.auth import Principal, StaticVerifier
    from copilot_gateway.config import Settings
    from copilot_gateway.router import build_router
    from copilot_gateway.session import Services
    from copilot_gateway.store import GatewayStore
    from copilot_gateway.testing import (
        GatewayServer,
        ProtocolClient,
        ScriptedProvider,
        TextSTT,
        TextSTTControl,
    )

    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        speculative_start_ms=100,
        silence_timer_ms=250,
        final_wait_max_ms=500,
        first_token_timeout_ms=1000,
        stt_stall_s=30.0,
        stt_keepalive_s=5.0,
        lease_renew_s=0.5,
        lease_ttl_s=5.0,
        flush_interval_s=600.0,
        first_message_timeout_s=2.0,
        llm_providers=["anthropic"],
        embedder="none",
    )
    server = FakeServer()
    them, me = TextSTTControl(), TextSTTControl()

    def stt(control: TextSTTControl) -> Any:
        def make(keyterms: Any) -> TextSTT:
            return TextSTT(control, "text")

        return make

    valkey = FakeAsyncValkey(server=server)
    if fx.mode == "standup" and fx.items:
        pack = standup_pack(fx)
        await valkey.set(keys.pack(EVAL_USER, pack.pack_id), pack.model_dump_json(by_alias=True))
        await valkey.hset(keys.pack_index(EVAL_USER), "mode:standup", str(pack.pack_id))  # pyright: ignore
    services = Services(
        settings=settings,
        store=GatewayStore(FakeAsyncValkey(server=server), settings),
        jobs=MemoryJobQueue(),
        router=build_router(settings, [ScriptedProvider("anthropic", oracle(fx))]),
        verifier=StaticVerifier({"token-a": Principal(EVAL_USER, user_name, None)}),
        stt_providers=[("text", stt(them))],
        stt_me_providers=[("text", stt(me))],
    )
    cards: list[Card] = []
    async with GatewayServer(services) as gw:
        client = ProtocolClient(gw.url)
        await client.start(mode=fx.mode)
        session = next(iter(gw.manager.sessions.values()))
        seen: set[Any] = set()

        async def settle() -> None:
            for _ in range(400):
                turns = session.turns
                busy = (turns.task is not None and not turns.task.done()) or bool(
                    turns._detached  # pyright: ignore[reportPrivateUsage]
                )
                if not busy:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.15)

        for line in fx.lines:
            start = len(client.messages)
            ms = int(line.t * 1000)
            channel = line.speaker
            if channel == "them":
                await client.say(line.text, capture_ms=ms)
            else:
                await client.say_me(line.text, capture_ms=ms, end_ms=ms + int(line.seconds * 1000))
            await client.wait(
                lambda m: m["type"] == "transcript.final" and m.get("channel") == channel,  # noqa: B023
                timeout=10.0,
                start=start,
            )
            await settle()
            for qid, q in list(session.answers.questions.items()):
                if qid in seen or q.held:
                    continue
                seen.add(qid)
                cards.append(Card(line.index, q.qtype, q.text))
            # A card that was shown and then taken back (the detector said no, or the
            # speaker went on) is gone from the engine: the client saw it come and go.
            gone = [m for m in client.messages[start:] if m["type"] == "question.cancelled"]
            cards += [Card(line.index, WITHDRAWN, "") for _ in gone]
        await client.send({"type": "session.end"})
        await client.closed()
    await valkey.aclose()
    return cards


def score_flow(fx: Fixture, cards: list[Card]) -> dict[str, Any]:
    by_line: dict[int, list[Card]] = {}
    withdrawn = [c for c in cards if c.qtype == WITHDRAWN]
    cards = [c for c in cards if c.qtype != WITHDRAWN]
    for c in cards:
        by_line.setdefault(c.line, []).append(c)
    checks: dict[str, list[tuple[int, bool]]] = {
        "question_one_card": [],
        "quiet": [],
        "whole_question": [],
        "follow": [],
        "update_card": [],
    }
    for line in fx.lines:
        got = by_line.get(line.index, [])
        if line.speaker == "them":
            if line.expect == "answer":
                checks["question_one_card"].append((line.index, len(got) == 1))
                if line.whole:
                    text = norm(" ".join(c.text for c in got))
                    checks["whole_question"].append(
                        (line.index, bool(got) and all(w in text for w in line.whole))
                    )
            elif line.expect == "none":
                checks["quiet"].append((line.index, not got))
        else:
            # A card that shows while the user speaks belongs to their line and the next.
            nxt = fx.lines[line.index + 1] if line.index + 1 < len(fx.lines) else None
            if line.update:
                later = (
                    by_line.get(nxt.index, []) if nxt is not None and nxt.speaker == "me" else []
                )
                shown = [c for c in (*got, *later) if c.qtype == "standup_status"]
                checks["update_card"].append((line.index, len(shown) == 1))
            elif line.follow == "card":
                checks["follow"].append((line.index, len(got) == 1 and got[0].qtype == "follow"))
            elif line.follow == "none":
                checks["follow"].append((line.index, not got))
    out: dict[str, Any] = {"cards": len(cards), "withdrawn": len(withdrawn)}
    failed: list[str] = [
        f"withdrawn: line {c.line} ({fx.lines[c.line].case or fx.lines[c.line].text[:50]})"
        for c in withdrawn
    ]
    for name, rows in checks.items():
        out[name] = [sum(ok for _, ok in rows), len(rows)]
        failed += [
            f"{name}: line {i} ({fx.lines[i].case or fx.lines[i].text[:50]})"
            for i, ok in rows
            if not ok
        ]
    out["failed"] = failed
    return out


# Detect: the configured detector on the labelled lines, live -----------------------------------


async def detect_line(
    provider: Any, model: str, fx: Fixture, line: Line, user_name: str, settings: Any
) -> tuple[Any, float, float | None]:
    """One detector call as the gateway sends it: (detection, ms, cost in USD)."""
    from copilot_core import prompts
    from copilot_core.llm import LLMDone
    from copilot_core.pricing import cost_usd
    from copilot_evals.detector_eval import reasoning_for
    from copilot_gateway.detector import parse_detection

    turns = ""
    if fx.mode in prompts.GROUP_MODES:
        spans = [
            ("them" if ln.speaker == "them" else "me", ln.t, ln.t + ln.seconds)
            for ln in fx.lines[: line.index]
        ]
        turns = prompts.turn_taking(user_name, spans, line.t)
    if settings.detector_api == "decisions":
        # As the gateway asks by default since 2026-11-16: the Decisions API, typed choices.
        from copilot_gateway.detector import detection_from

        dreq = prompts.detector_decision(
            user_name, transcript_before(fx, line, user_name), question_text(fx, line),
            fx.mode, turns,
        )  # fmt: skip
        started = time.monotonic()
        decided = await provider.decide(settings.decisions_model, dreq.input, dreq.questions)
        ms = (time.monotonic() - started) * 1000
        u = decided.usage
        price = cost_usd(
            settings.decisions_model, "standard", u.input_tokens, u.cached_tokens, 0,
            settings.model_prices, settings.tier_price_multipliers,
            cache_write_tokens=getattr(u, "cache_write_tokens", None),
            cache_write_30m_tokens=getattr(u, "cache_write_30m_tokens", None),
            cache_write_12h_tokens=getattr(u, "cache_write_12h_tokens", None),
        )  # fmt: skip
        return (
            detection_from(decided.answers, fx.mode)[0],
            ms,
            (float(price) if price is not None else None),
        )
    req = prompts.detector(
        user_name,
        transcript_before(fx, line, user_name),
        question_text(fx, line),
        80,
        fx.mode,
        turns,
    )
    effort = reasoning_for(model)
    params: dict[str, Any] = {} if effort is None else {"reasoning": {"effort": effort}}
    started = time.monotonic()
    parts: list[str] = []
    cost: float | None = None
    async for chunk in provider.stream(model, req, params):
        if isinstance(chunk, LLMDone):
            cost = call_cost(settings, chunk)
        else:
            parts.append(chunk)
    return parse_detection("".join(parts)), (time.monotonic() - started) * 1000, cost


async def run_detect(fixtures: list[Fixture], user_name: str, reps: int) -> dict[str, Any]:
    from copilot_core.dotenv import load_dotenv
    from copilot_core.llm import OpenAIProvider
    from copilot_core.providers import openai_client
    from copilot_gateway.config import Settings

    load_dotenv()
    settings = Settings()
    provider = OpenAIProvider("openai", openai_client(settings))
    await provider.warm("")
    model = settings.openai_route("detector")[0]
    fired = expected = false_fires = quiet = type_right = type_total = 0
    false_by: dict[str, list[int]] = {}
    wrong: list[str] = []
    latencies: list[float] = []
    cost = summarize_costs([])
    for _ in range(reps):
        for fx in fixtures:
            for line in fx.lines:
                if line.speaker != "them" or (line.expect == "either" and not line.unfinished):
                    continue
                detection, ms, c = await detect_line(provider, model, fx, line, user_name, settings)
                latencies.append(ms)
                add_cost(cost, c)
                should = line.expect == "answer" and not line.unfinished
                got = detection.fires()
                where = f"{fx.id} line {line.index}: {detection.decision}/{detection.qtype}"
                if should:
                    expected += 1
                    fired += int(got)
                    if not got:
                        wrong.append(f"missed {where} ({line.case or line.text[:50]})")
                    elif line.qtype:
                        type_total += 1
                        type_right += int(detection.qtype == line.qtype)
                        if detection.qtype != line.qtype:
                            wrong.append(f"type {where}, wanted {line.qtype}")
                else:
                    quiet += 1
                    by = false_by.setdefault(line.addressee, [0, 0])
                    by[1] += 1
                    if got:
                        false_fires += 1
                        by[0] += 1
                        wrong.append(f"false fire {where} ({line.case or line.text[:50]})")
    latencies.sort()
    return {
        "model": model,
        "api": settings.detector_api,
        "reps": reps,
        "fired": [fired, expected],
        "false_fires": [false_fires, quiet],
        "false_fires_by_addressee": false_by,
        "type": [type_right, type_total],
        "latency_p50_ms": round(latencies[len(latencies) // 2]) if latencies else None,
        "calls": len(latencies),
        **cost,
        "wrong": wrong,
    }


# Answers: the short (and detailed) answer on the lines with checks, live ------------------------


def apply_check(check: Check, answer: str) -> list[tuple[str, bool]]:
    out = [(f"must /{p}/", re.search(p, answer) is not None) for p in check.must]
    out += [(f"must not /{p}/", re.search(p, answer) is None) for p in check.must_not]
    return out


def prompt_context(fx: Fixture, user_name: str) -> Any:
    from copilot_core.packs import render_pack
    from copilot_core.prompts import PromptContext

    if fx.mode == "interview":
        from copilot_evals.answer_eval import _context  # pyright: ignore[reportPrivateUsage]

        return _context()[0]
    pack_text = render_pack(standup_pack(fx)) if fx.items else None
    return PromptContext(user_name, fx.mode, None, pack_text, company=fx.company, role=fx.role)


async def run_answers(
    fixtures: list[Fixture], user_name: str, reps: int, deep: bool
) -> dict[str, Any]:
    from copilot_core import prompts
    from copilot_core.dotenv import load_dotenv
    from copilot_gateway.config import Settings
    from copilot_gateway.router import build_router

    load_dotenv()
    settings = Settings()
    router = build_router(settings)
    await router.warm()
    passed = total = 0
    cost = summarize_costs([])
    failed: list[dict[str, str]] = []
    per_pass: dict[str, list[int]] = {}
    for rep in range(reps):
        for fx in fixtures:
            ctx = prompt_context(fx, user_name)
            for line in fx.lines:
                if line.check is None:
                    continue
                call = prompts.CallContext(recent=transcript_before(fx, line, user_name))
                question = question_text(fx, line)
                req = prompts.fast_pass(ctx, call, question, None, settings.fast_pass_max_tokens)
                fast, done = await router.complete("fast", req)
                add_cost(cost, call_cost(settings, done))
                answers = {"fast": str(fast).strip()}
                if deep:
                    req = prompts.deep_pass(
                        ctx, call, question, line.qtype or "other", settings.deep_pass_max_tokens,
                        shown=answers["fast"],
                    )  # fmt: skip
                    text, done = await router.complete("deep", req)
                    add_cost(cost, call_cost(settings, done))
                    answers["deep"] = str(text).strip()
                for pass_, answer in answers.items():
                    tally = per_pass.setdefault(pass_, [0, 0])
                    for name, ok in apply_check(line.check, answer):
                        total += 1
                        tally[1] += 1
                        passed += int(ok)
                        tally[0] += int(ok)
                        if not ok:
                            failed.append(
                                {"where": f"{fx.id} line {line.index} {pass_} rep {rep}",
                                 "check": name, "answer": answer}
                            )  # fmt: skip
    return {
        "reps": reps,
        "checks": [passed, total],
        "by_pass": per_pass,
        **cost,
        "failed": failed,
    }


# Commands ------------------------------------------------------------------------------------


def ratio(pair: Any) -> str:
    a, b = pair
    return f"{a}/{b}" if b else "—"


def summary_rows(s: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    flow = cast(dict[str, Any] | None, s.get("flow"))
    if flow:
        t = flow["total"]
        rows += [
            ("Flow: one card per question", ratio(t["question_one_card"])),
            ("Flow: no card where none is due", ratio(t["quiet"])),
            ("Flow: the whole question on the card", ratio(t["whole_question"])),
            ("Flow: follow cards right", ratio(t["follow"])),
            ("Flow: update card in a standup", ratio(t["update_card"])),
            ("Flow: all checks", ratio(t["all"])),
            (
                "Flow: cards shown, then withdrawn",
                str(sum(int(f.get("withdrawn", 0)) for f in flow["by_fixture"].values())),
            ),
        ]
    det = cast(dict[str, Any] | None, s.get("detect"))
    if det:
        rows += [
            ("Detect: questions for me fired", ratio(det["fired"])),
            ("Detect: false fires", ratio(det["false_fires"])),
            ("Detect: type right", ratio(det["type"])),
            ("Detect: latency p50 (ms)", str(det["latency_p50_ms"])),
        ]
    ans = cast(dict[str, Any] | None, s.get("answers"))
    if ans:
        rows.append(("Answers: checks passed", ratio(ans["checks"])))
        for pass_, pair in cast(dict[str, Any], ans["by_pass"]).items():
            rows.append((f"Answers: {pass_}", ratio(pair)))
    rows.append(("Cost (USD)", format_cost(s)))
    return rows


def table(labels: list[str]) -> str:
    summaries = [json.loads((RESULTS / lb / "summary.json").read_text("utf-8")) for lb in labels]
    names: list[str] = []
    for s in summaries:
        names += [n for n, _ in summary_rows(s) if n not in names]
    lines = ["| | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for name in names:
        cells = [dict(summary_rows(s)).get(name, "—") for s in summaries]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def cmd_run(args: argparse.Namespace) -> int:
    from copilot_evals.benchdata import load_profile

    user_name = load_profile().user_name
    fixtures = load_fixtures(args.fixture)
    parts = args.part or list(PARTS)
    summary: dict[str, Any] = {"fixtures": [f.id for f in fixtures], "cost_usd": 0.0}
    if "flow" in parts:
        flow: dict[str, Any] = {"by_fixture": {}}
        total: dict[str, list[int]] = {}
        for fx in fixtures:
            scored = score_flow(fx, await replay(fx, user_name))
            flow["by_fixture"][fx.id] = scored
            for k, v in scored.items():
                if isinstance(v, list) and len(cast(list[Any], v)) == 2 and k != "failed":
                    t = total.setdefault(k, [0, 0])
                    t[0] += int(cast(list[int], v)[0])
                    t[1] += int(cast(list[int], v)[1])
        total["all"] = [sum(v[0] for v in total.values()), sum(v[1] for v in total.values())]
        flow["total"] = total
        summary["flow"] = flow
    if "detect" in parts:
        summary["detect"] = await run_detect(fixtures, user_name, args.reps)
    if "answers" in parts:
        summary["answers"] = await run_answers(fixtures, user_name, args.reps, args.deep)
    summary.update(
        summarize_costs([summary[part] for part in ("detect", "answers") if part in summary])
    )
    out = RESULTS / args.label
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    for name, value in summary_rows(summary):
        print(f"{name:<40} {value}")
    for part in ("flow", "detect", "answers"):
        block = cast(dict[str, Any], summary.get(part) or {})
        fails: list[Any] = []
        if part == "flow":
            for fid, scored in cast(dict[str, Any], block.get("by_fixture", {})).items():
                fails += [f"{fid} {f}" for f in scored["failed"]]
        elif part == "detect":
            fails = block.get("wrong", [])
        else:
            fails = [f"{f['where']}: {f['check']}" for f in block.get("failed", [])]
        for f in fails:
            print(f"  {part}: {f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score paraphrased sessions: flow, detect, answers.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--part", action="append", choices=PARTS, help="default: all three")
    r.add_argument("--fixture", action="append", help="a fixture id (repeatable)")
    r.add_argument("--reps", type=int, default=2, help="repetitions of the live parts")
    r.add_argument("--deep", action="store_true", help="check the detailed answer too")
    c = sub.add_parser("compare")
    c.add_argument("labels", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "compare":
        print(table(args.labels))
        return 0
    return asyncio.run(cmd_run(args))


def run() -> None:
    sys.exit(main())
