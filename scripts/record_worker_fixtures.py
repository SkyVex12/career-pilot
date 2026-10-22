"""Re-record the worker's provider fixtures in apps/worker/tests/fixtures from the real services.

    uv run python scripts/record_worker_fixtures.py                    # Claude, every job
    uv run python scripts/record_worker_fixtures.py --only extract pack
    uv run python scripts/record_worker_fixtures.py --provider openai  # OpenAI, every job

Needs ANTHROPIC_API_KEY (the direct API, so recordings carry no Bedrock specifics), or
OPENAI_API_KEY and the COPILOT_OPENAI_<JOB>_MODEL settings for --provider openai; from the
environment or .env. Claude recordings go to
fixtures/claude/, OpenAI recordings to fixtures/openai/ (Responses API events).

Each model request is the one the worker sends (copilot_worker.prompts builds both), sent
through the official SDK over a transport that keeps the raw response bytes:

- extract, job: the sample resume and job description in fixtures/inputs.
- pack: a digest rendered from the recorded extraction (as digest.rebuild would), plus the
  job description. Short ids in the output are written as {{ref:N}}, N being the id's position
  in that digest, so the replay can put the run's own ids back (copilot_worker.fixtures).
- postcall_interview, postcall_standup, postcall_meeting: the sample transcripts below.

Recordings hold no secrets, only the sample documents' and transcripts' content.
"""

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai

from copilot_core.dotenv import load_dotenv
from copilot_core.llm import AnthropicProvider, LLMDone, LLMProvider, LLMRequest, OpenAIProvider
from copilot_worker import prompts
from copilot_worker.config import WorkerSettings
from copilot_worker.documents import document_block
from copilot_worker.fixtures import SHORT_ID
from copilot_worker.llm import build_routes
from copilot_worker.memory import render_digest

FIXTURES = Path(__file__).resolve().parent.parent / "apps" / "worker" / "tests" / "fixtures"
INPUTS = FIXTURES / "inputs"
SHORT_ID_TEXT = re.compile(r"n:[0-9a-z]{2,8}")

INTERVIEW = "\n".join(
    [
        "[00:01] them: Thanks for joining. Tell me about a migration of a critical data store"
        " you led.",
        "[00:12] me: At Acme I moved our payments ledger from MySQL to PostgreSQL with dual"
        " writes and a shadow read path.",
        "[00:58] them: How would you design an idempotent payout API?",
        "[01:10] me: An idempotency key on every request and a state machine per payout.",
    ]
)
INTERVIEW_LOG = "\n".join(
    [
        "Q1 (behavioral): Tell me about a migration of a critical data store you led.",
        "Suggested (fast): - Moved the ledger to PostgreSQL live [n:aaaa]",
        "Q2 (system_design): How would you design an idempotent payout API?",
        "Suggested (fast): - Idempotency keys on every payout request [n:bbbb]",
    ]
)
STANDUP = "\n".join(
    [
        "[00:01] them: Alex, your update?",
        "[00:03] me: Yesterday I finished the payout retry fix. Today I'm reviewing the ledger"
        " index migration.",
        "[00:15] me: I'm blocked on database access for staging. I'll send the migration plan to"
        " the team by tomorrow.",
    ]
)
MEETING = "\n".join(
    [
        "[00:01] them: Let's decide how to roll out the payout service.",
        "[00:20] me: Behind a flag, starting with one percent of payouts. I'll write the rollout"
        " plan.",
    ]
)


class Recorder(httpx2.AsyncBaseTransport):
    """Forwards to the network and keeps the last response body, byte for byte."""

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.body = b""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        self.body = await response.aread()
        return httpx2.Response(response.status_code, headers=response.headers, content=self.body)


def recorded_text(path: Path) -> str:
    """The output text of a recorded stream, Claude's or OpenAI's."""
    out: list[str] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event.get("type") == "content_block_delta":
            out.append(event["delta"].get("text", ""))
        elif event.get("type") == "response.output_text.delta":
            out.append(event["delta"])
    return "".join(out)


def keep_short_ids_whole(body: str) -> str:
    """OpenAI streams a few characters per delta, so a short id (`n:3f2a`) can span several
    deltas, where the {{ref:N}} replacement cannot find it. Move each delta boundary that
    falls inside a short id to its end: the same events and text, other boundaries."""
    lines = body.split("\n")
    deltas = [
        (i, json.loads(line[6:]))
        for i, line in enumerate(lines)
        if line.startswith("data: ") and '"response.output_text.delta"' in line
    ]
    text = "".join(e["delta"] for _, e in deltas)
    inside = {p for m in SHORT_ID_TEXT.finditer(text) for p in range(m.start() + 1, m.end())}
    cuts, at = [], 0
    for _, e in deltas:
        at += len(e["delta"])
        while at in inside:
            at += 1
        cuts.append(max(at, cuts[-1] if cuts else 0))
    start = 0
    for (i, e), end in zip(deltas, cuts, strict=True):
        e["delta"] = text[start:end]
        lines[i] = "data: " + json.dumps(e, separators=(",", ":"), ensure_ascii=False)
        start = end
    return "\n".join(lines)


async def record(
    provider: LLMProvider, recorder: Recorder, model: str, params: dict[str, Any],
    req: LLMRequest, name: str, refs: list[str] | None = None,
) -> str:  # fmt: skip
    parts: list[str] = []
    done: LLMDone | None = None
    async for chunk in provider.stream(model, req, params):
        if isinstance(chunk, LLMDone):
            done = chunk
        else:
            parts.append(chunk)
    assert done is not None
    if done.stop_reason != "end_turn":
        sys.exit(f"{name}: stop reason {done.stop_reason}; not saved")
    body = recorder.body.decode("utf-8")
    if refs and provider.name == "openai":
        body = keep_short_ids_whole(body)
    for i, short in enumerate(refs or []):
        body = body.replace(short, f"{{{{ref:{i}}}}}")
    wire = "openai" if provider.name == "openai" else "claude"
    (FIXTURES / wire / f"{name}.sse").write_text(body, "utf-8", newline="\n")
    u = done.usage
    print(
        f"wrote {wire}/{name}.sse ({len(body)} bytes, {model}, {u.input_tokens} in, "
        f"{u.cached_tokens} cached, {u.output_tokens} out, {done.first_token_ms} ms)"
    )
    return "".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-record the worker's provider fixtures.")
    jobs = ["extract", "job", "pack", "postcall_interview", "postcall_standup", "postcall_meeting"]
    parser.add_argument("--only", nargs="+", choices=jobs, default=jobs)
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    args = parser.parse_args()
    load_dotenv()
    settings = WorkerSettings(llm_providers=[args.provider])  # pyright: ignore[reportCallIssue]
    recorder = Recorder()
    provider: LLMProvider
    if args.provider == "openai":
        if not settings.openai_api_key or not settings.openai_api_key.get_secret_value():
            sys.exit("OPENAI_API_KEY is not set")
        provider = OpenAIProvider(
            "openai",
            openai.AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                http_client=openai.DefaultAsyncHttpxClient(transport=recorder),
                timeout=600,
                max_retries=0,
            ),
        )
    else:
        if not settings.anthropic_api_key or not settings.anthropic_api_key.get_secret_value():
            sys.exit("ANTHROPIC_API_KEY is not set")
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            http_client=anthropic.DefaultAsyncHttpxClient(transport=recorder),
            timeout=600,
        )
        provider = AnthropicProvider("anthropic", client)
    routes = build_routes(settings, [provider])

    def route(job: str) -> tuple[str, dict[str, Any]]:
        r = routes[job][0]
        return r.model, dict(r.params)

    resume = document_block((INPUTS / "resume.md").read_bytes(), "text/markdown", "Resume")
    job_md = (INPUTS / "job.md").read_bytes()
    extract_path = FIXTURES / ("openai" if args.provider == "openai" else "claude") / "extract.sse"
    if "extract" in args.only:
        await record(
            provider, recorder, *route("extract"), prompts.resume_request(resume), "extract"
        )
    if "job" in args.only:
        doc = document_block(job_md, "text/markdown", "Job description")
        await record(provider, recorder, *route("job"), prompts.job_request(doc), "job")
    if "pack" in args.only:
        # The digest the pack is built from: the recorded extraction, as nodes in order.
        data = json.loads(recorded_text(extract_path))
        nodes: list[dict[str, Any]] = []
        for kind, items in (("role", data["roles"]), ("project", data["projects"]),
                            ("story", data["stories"]), ("skill", data["skills"])):  # fmt: skip
            for item in items:
                title = item.get("title") or item.get("name")
                if kind == "role":
                    title = f"{item['title']}, {item['company']}"
                nodes.append({"id": uuid.uuid4(), "kind": kind, "title": title,
                              "summary": item.get("one_liner") or item.get("summary") or title,
                              "body": item})  # fmt: skip
        nodes.sort(key=lambda n: n["id"])  # creation order is id order
        digest = render_digest(data.get("user_name", "Alex"), nodes)
        assert digest is not None
        refs = list(dict.fromkeys(SHORT_ID.findall(digest.text)))
        req = prompts.pack_request(
            digest.text, "Globex", "Staff Backend Engineer", "technical", job_md.decode(), 30
        )
        await record(provider, recorder, *route("pack"), req, "pack", refs)
    for mode, transcript, log in (("interview", INTERVIEW, INTERVIEW_LOG),
                                  ("standup", STANDUP, ""), ("meeting", MEETING, "")):  # fmt: skip
        name = f"postcall_{mode}"
        if name in args.only:
            context = ["User: Alex Doe", f"Mode: {mode}", "Length: 15 min"]
            req = prompts.postcall_request(mode, context, transcript, log)
            await record(provider, recorder, *route("postcall"), req, name)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
