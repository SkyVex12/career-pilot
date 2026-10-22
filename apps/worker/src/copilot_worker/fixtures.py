"""Replay recorded model responses through the real clients.

Tests, and a worker started with COPILOT_FIXTURES_DIR (dev and test only), send every model
request through the official SDK (Anthropic or OpenAI), but over a mock transport that
answers from files instead of the network:

    <dir>/claude/<job>.sse          streaming Messages API events, byte for byte
    <dir>/openai/<job>.sse          streaming Responses API events, byte for byte

The job is recognized by the first line of the request's system prompt (each worker prompt
starts with its own line; see `JOB_PROMPTS`): Claude's `system`, OpenAI's developer message.
`scripts/record_worker_fixtures.py` re-records the files from the real services.

Short ids (`n:3f2a`) come from node ids made at run time, so a recording cannot hold them.
Recorded output writes the Nth short id of the request's `<digest>` block as `{{ref:N}}`,
and the replay puts this run's id back in its place.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai

from copilot_core.llm import AnthropicProvider, LLMProvider, OpenAIProvider

DIGEST_BLOCK = re.compile(r"<digest>(.*?)</digest>", re.S)
SHORT_ID = re.compile(r"\[(n:[0-9a-z]{2,8})\]")
REF = re.compile(rb"\{\{ref:(\d+)\}\}")


def digest_refs(body: Mapping[str, Any]) -> list[str]:
    """Short ids in the order the request's digest first cites them."""
    turns = body.get("messages", body.get("input", []))
    found = DIGEST_BLOCK.search(json.dumps(turns, ensure_ascii=False))
    ids = SHORT_ID.findall(found.group(1)) if found else []
    return list(dict.fromkeys(ids))


class FixtureError(Exception):
    pass


class ClaudeFixtures(httpx2.AsyncBaseTransport):
    wire = "claude"

    def __init__(self, directory: Path, job_prompts: Mapping[str, str]) -> None:
        self.directory = directory
        self.job_prompts = dict(job_prompts)
        self.requests: list[tuple[str, dict[str, Any]]] = []  # (job, request body)

    def first_system_text(self, body: Mapping[str, Any]) -> str:
        system: list[Any] = body.get("system") or []
        return str(system[0].get("text", "")) if system else ""

    def job_for(self, body: Mapping[str, Any]) -> str:
        first = self.first_system_text(body)
        for job, prefix in self.job_prompts.items():
            if first.startswith(prefix):
                return job
        raise FixtureError(f"no fixture job for system prompt {first[:60]!r}")

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(await request.aread())
        job = self.job_for(body)
        self.requests.append((job, body))
        path = self.directory / self.wire / f"{job}.sse"
        if not path.is_file():
            raise FixtureError(f"missing fixture {path}")
        refs = digest_refs(body)

        def fill(m: re.Match[bytes]) -> bytes:
            n = int(m.group(1))
            return refs[n].encode() if n < len(refs) else b"n:none"

        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=REF.sub(fill, path.read_bytes()),
        )


class OpenAIFixtures(ClaudeFixtures):
    """The same replay for OpenAI's Responses API: files under `<dir>/openai/`."""

    wire = "openai"

    def first_system_text(self, body: Mapping[str, Any]) -> str:
        turns: list[Any] = body.get("input") or []
        if not turns or turns[0].get("role") != "developer":
            return ""
        content: list[Any] = turns[0].get("content") or []
        return str(content[0].get("text", "")) if content else ""


def model_fixtures(directory: Path, job_prompts: Mapping[str, str], wire: str) -> ClaudeFixtures:
    return (OpenAIFixtures if wire == "openai" else ClaudeFixtures)(directory, job_prompts)


def fixture_provider(transport: ClaudeFixtures) -> LLMProvider:
    if isinstance(transport, OpenAIFixtures):
        http = openai.DefaultAsyncHttpxClient(transport=transport)
        return OpenAIProvider(
            "openai", openai.AsyncOpenAI(api_key="fixture", max_retries=0, http_client=http)
        )
    client = anthropic.AsyncAnthropic(
        api_key="fixture",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
    )
    return AnthropicProvider("anthropic", client)
