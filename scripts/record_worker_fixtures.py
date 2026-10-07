"""Re-record the worker's provider fixtures in apps/worker/tests/fixtures from the real services.

    uv run python scripts/record_worker_fixtures.py                    # Claude, every job
    uv run python scripts/record_worker_fixtures.py --only extract pack
    uv run python scripts/record_worker_fixtures.py --deepgram me.wav  # also Deepgram

Needs ANTHROPIC_API_KEY (the direct API, so recordings carry no Bedrock specifics) and, for
Deepgram, DEEPGRAM_API_KEY; from the environment or .env.

Each Claude request is the one the worker sends (copilot_worker.prompts builds both), sent
through the official SDK over a transport that keeps the raw response bytes:

- extract, job: the sample resume and job description in fixtures/inputs.
- pack: a digest rendered from the recorded extraction (as digest.rebuild would), plus the
  job description. Short ids in the output are written as {{ref:N}}, N being the id's position
  in that digest, so the replay can put the run's own ids back (copilot_worker.fixtures).
- postcall_interview, postcall_standup, postcall_meeting: the sample transcripts below.
- deepgram/prerecorded.json: the /v1/listen response for --deepgram's file.

Recordings hold no secrets, only the sample documents' and transcripts' content.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import anthropic
import httpx2

from copilot_core.dotenv import load_dotenv
from copilot_core.llm import AnthropicProvider, LLMDone, LLMRequest
from copilot_worker import prompts
from copilot_worker.config import WorkerSettings
from copilot_worker.documents import document_block
from copilot_worker.fixtures import SHORT_ID
from copilot_worker.llm import build_routes
from copilot_worker.memory import render_digest
from copilot_worker.stt import DeepgramPrerecorded

FIXTURES = Path(__file__).resolve().parent.parent / "apps" / "worker" / "tests" / "fixtures"
INPUTS = FIXTURES / "inputs"

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


async def record(
    provider: AnthropicProvider, recorder: Recorder, model: str, params: dict[str, Any],
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
    for i, short in enumerate(refs or []):
        body = body.replace(short, f"{{{{ref:{i}}}}}")
    (FIXTURES / "claude" / f"{name}.sse").write_text(body, "utf-8", newline="\n")
    print(f"wrote claude/{name}.sse ({len(body)} bytes, {done.usage.output_tokens} output tokens)")
    return "".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-record the worker's provider fixtures.")
    jobs = ["extract", "job", "pack", "postcall_interview", "postcall_standup", "postcall_meeting"]
    parser.add_argument("--only", nargs="+", choices=jobs, default=jobs)
    parser.add_argument("--deepgram", type=Path, help="audio file to send to Deepgram")
    args = parser.parse_args()
    load_dotenv()
    settings = WorkerSettings(llm_providers=["anthropic"])  # pyright: ignore[reportCallIssue]
    if not settings.anthropic_api_key or not settings.anthropic_api_key.get_secret_value():
        sys.exit("ANTHROPIC_API_KEY is not set")
    recorder = Recorder()
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
    extract_path = FIXTURES / "claude" / "extract.sse"
    if "extract" in args.only:
        await record(
            provider, recorder, *route("extract"), prompts.resume_request(resume), "extract"
        )
    if "job" in args.only:
        doc = document_block(job_md, "text/markdown", "Job description")
        await record(provider, recorder, *route("job"), prompts.job_request(doc), "job")
    if "pack" in args.only:
        # The digest the pack is built from: the recorded extraction, as nodes in order.
        text = "".join(
            json.loads(line[6:])["delta"].get("text", "")
            for line in extract_path.read_text("utf-8").splitlines()
            if line.startswith("data: ") and '"text_delta"' in line
        )
        data = json.loads(text)
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
    if args.deepgram:
        if not settings.deepgram_api_key or not settings.deepgram_api_key.get_secret_value():
            sys.exit("DEEPGRAM_API_KEY is not set")
        keep = Recorder()
        stt = DeepgramPrerecorded(settings.deepgram_api_key.get_secret_value(), transport=keep)
        segments = await stt.transcribe(args.deepgram.read_bytes(), "audio/wav")
        data = json.loads(keep.body)
        (FIXTURES / "deepgram" / "prerecorded.json").write_text(
            json.dumps(data, indent=2) + "\n", "utf-8", newline="\n"
        )
        print(f"wrote deepgram/prerecorded.json ({len(segments)} utterances)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
