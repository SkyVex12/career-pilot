"""Re-record the provider fixtures in apps/gateway/tests/fixtures from the real services.

    uv run python scripts/record_fixtures.py --wav clip.wav            # all
    uv run python scripts/record_fixtures.py --only anthropic
    uv run python scripts/record_fixtures.py --only openai

Needs ANTHROPIC_API_KEY (direct API), OPENAI_API_KEY and DEEPGRAM_API_KEY, from the
environment or .env. OpenAI uses the models set for the detector, fast and deep jobs
(COPILOT_OPENAI_<JOB>_MODEL).
- anthropic_fast.sse: the raw event stream of one fast-pass request, byte for byte.
- openai_fast.sse, openai_deep.sse, openai_detector.sse: raw Responses API event streams of a
  fast pass, a deep pass and a detector call (structured output), sent through
  `OpenAIProvider` exactly as the gateway sends them. The fast pass goes twice and the second
  response is kept, so it shows a prompt cache hit when OpenAI gives one.
- deepgram_session.json: every message Deepgram sends for the first utterance of the clip,
  split into those that arrived before and after the Finalize message.
The adapter tests derive their expectations from these files, so they keep passing.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai

from copilot_core import prompts
from copilot_core.dotenv import load_dotenv
from copilot_core.llm import LLMDone, LLMRequest, OpenAIProvider
from copilot_core.providers import openai_params
from copilot_gateway.config import Settings
from copilot_gateway.router import job_params
from copilot_gateway.stt.deepgram import DeepgramSTT

FIXTURES = Path(__file__).resolve().parent.parent / "apps" / "gateway" / "tests" / "fixtures"

DIGEST = """# Career digest (version fixture)
## Roles
[n:7c1e] Senior Backend Engineer, Acme Payments, 2021 to now. Team: Payments Platform.
## Top stories
[n:3f2a] Payments migration: moved the ledger to a new store with zero downtime; p95 latency
down 40%. Competencies: ownership, technical depth.
## Skills
Python (9 y), PostgreSQL (8 y), AWS (6 y), Go (3 y)
"""


async def record_anthropic(settings: Settings) -> None:
    if settings.anthropic_api_key is None:
        sys.exit("ANTHROPIC_API_KEY is not set")
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    ctx = prompts.PromptContext("Alex", "interview", DIGEST, "Mode: interview")
    req = prompts.fast_pass(
        ctx, "[00:12] Them: How did you scale the ledger?", "How did you scale the ledger?",
        "technical", settings.fast_pass_max_tokens,
    )  # fmt: skip
    params = job_params(settings, "anthropic", "fast")
    body: dict[str, Any] = {
        "model": settings.haiku_model,
        "max_tokens": req.max_tokens,
        "system": [
            {"type": "text", "text": b.text}
            | ({"cache_control": {"type": "ephemeral"}} if b.cache else {})
            for b in req.system
        ],
        "messages": [{"role": "user", "content": req.content}],
        **params,
    }
    chunks: list[bytes] = []
    async with client.messages.with_streaming_response.create(**body, stream=True) as response:
        async for chunk in response.iter_bytes():
            chunks.append(chunk)
    (FIXTURES / "anthropic_fast.sse").write_bytes(b"".join(chunks))
    print(f"wrote anthropic_fast.sse ({sum(map(len, chunks))} bytes)")


class Recorder(httpx2.AsyncBaseTransport):
    """Forwards to the network and keeps the last response body, byte for byte."""

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.body = b""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        self.body = await response.aread()
        return httpx2.Response(response.status_code, headers=response.headers, content=self.body)


def fixture_context() -> prompts.PromptContext:
    # A cacheable prefix over OpenAI's 1,024-token minimum, as a real digest and pack are.
    pack = "Mode: interview\n" + "\n".join(
        f"Q: Prepared question {i} about the ledger migration?\n- Point [n:3f2a]" for i in range(60)
    )
    return prompts.PromptContext("Alex", "interview", DIGEST, pack)


async def record_openai(settings: Settings) -> None:
    if settings.openai_api_key is None:
        sys.exit("OPENAI_API_KEY is not set")
    recorder = Recorder()
    client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        http_client=openai.DefaultAsyncHttpxClient(transport=recorder),
        max_retries=0,
    )
    provider = OpenAIProvider("openai", client)
    ctx = fixture_context()
    question = "How did you scale the ledger?"
    transcript = f"[00:12] Them: {question}"
    requests: list[tuple[str, str, LLMRequest, int]] = [
        ("detector", "detector", prompts.detector("Alex", transcript, question, 80), 1),
        ("fast", "fast", prompts.fast_pass(ctx, transcript, question, "technical", 250), 2),
        ("deep", "deep", prompts.deep_pass(ctx, transcript, question, "technical", 800), 1),
    ]
    for name, job, req, times in requests:
        model, _ = settings.openai_route(job)
        done: LLMDone | None = None
        for _ in range(times):
            async for chunk in provider.stream(model, req, openai_params(settings, job)):
                if isinstance(chunk, LLMDone):
                    done = chunk
        assert done is not None
        if done.stop_reason != "end_turn":
            sys.exit(f"openai_{name}: stop reason {done.stop_reason}; not saved")
        (FIXTURES / f"openai_{name}.sse").write_bytes(recorder.body)
        u = done.usage
        print(
            f"wrote openai_{name}.sse ({len(recorder.body)} bytes, {model}, "
            f"{u.input_tokens} in / {u.cached_tokens} cached / {u.output_tokens} out)"
        )


async def record_deepgram(settings: Settings, wav: Path) -> None:
    from copilot_evals.replay import load_wav
    from copilot_evals.vad import segment, silero_probabilities

    if settings.deepgram_api_key is None:
        sys.exit("DEEPGRAM_API_KEY is not set")
    pcm = load_wav(wav)
    segs = segment(silero_probabilities(pcm), total_samples=len(pcm) // 2)
    if not segs:
        sys.exit(f"{wav}: no speech found")
    seg = segs[0]
    audio = pcm[max(0, seg.start_edge - 3200) * 2 : seg.end_decided * 2]
    stt = DeepgramSTT(
        settings.deepgram_api_key.get_secret_value(),
        url=settings.deepgram_url,
        model=settings.deepgram_model,
    )
    await stt.connect()
    assert stt.ws is not None
    on_audio: list[dict[str, Any]] = []
    on_finalize: list[dict[str, Any]] = []
    finalized = asyncio.Event()

    async def read() -> None:
        assert stt.ws is not None
        async for raw in stt.ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            (on_finalize if finalized.is_set() else on_audio).append(msg)
            if msg.get("from_finalize"):
                return

    reader = asyncio.create_task(read())
    for i in range(0, len(audio), 3200):  # 100 ms frames in real time
        await stt.send_audio(audio[i : i + 3200])
        await asyncio.sleep(0.1)
    finalized.set()
    await stt.finalize()
    await asyncio.wait_for(reader, 10)
    await stt.close()
    fixture = {
        "note": f"Recorded from Deepgram {settings.deepgram_model} by scripts/record_fixtures.py",
        "on_audio": on_audio,
        "on_finalize": on_finalize,
    }
    (FIXTURES / "deepgram_session.json").write_text(json.dumps(fixture, indent=1), "utf-8")
    print(f"wrote deepgram_session.json ({len(on_audio)} + {len(on_finalize)} messages)")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-record provider fixtures.")
    parser.add_argument("--wav", type=Path, help="clip whose first utterance is sent to Deepgram")
    parser.add_argument("--only", choices=["anthropic", "openai", "deepgram"])
    args = parser.parse_args()
    load_dotenv()
    settings = Settings()
    if args.only in (None, "anthropic"):
        await record_anthropic(settings)
    if args.only in (None, "openai"):
        await record_openai(settings)
    if args.only in (None, "deepgram"):
        if args.wav is None:
            parser.error("--wav is needed to record Deepgram")
        await record_deepgram(settings, args.wav)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
