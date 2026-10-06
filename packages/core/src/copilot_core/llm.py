"""Model routing layer (ARCHITECTURE.md, "Models and routing" and "Failure modes").

Every Claude call goes through `ModelRouter`: a job name ("detector", "fast", "deep", ...)
maps to an ordered list of routes. A route is tried until it yields its first token; an error,
or no first token within `first_token_timeout_s`, moves on to the next route. Once text has
streamed, an error is final, since the user has already seen that text.

Providers wrap the official Anthropic SDK: `AsyncAnthropic` for the direct API and
`AsyncAnthropicBedrockMantle` for Bedrock. Both expose the same `messages.stream` surface.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SystemBlock:
    text: str
    cache: bool = False  # put a cache breakpoint after this block


@dataclass(frozen=True)
class LLMRequest:
    system: Sequence[SystemBlock]
    # A user turn: plain text, or content blocks (for example a PDF document block).
    content: str | Sequence[Mapping[str, Any]]
    max_tokens: int
    json_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0  # all input tokens, cached ones included
    cached_tokens: int = 0  # read from the prompt cache
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMDone:
    usage: LLMUsage
    stop_reason: str | None
    provider: str = ""
    model: str = ""
    attempt: int = 0  # index of the route that answered; above 0 means a fallback ran
    first_token_ms: int = 0


type LLMChunk = str | LLMDone


class LLMProvider(Protocol):
    name: str

    def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        """Text deltas, then exactly one LLMDone."""
        ...


@dataclass(frozen=True)
class Route:
    provider: LLMProvider
    model: str
    # Extra request fields for this route, for example thinking or output_config.effort.
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.model}"


class AllRoutesFailedError(Exception):
    def __init__(self, job: str, errors: list[str]) -> None:
        super().__init__(f"{job}: every route failed: {'; '.join(errors)}")
        self.errors = errors


class FirstTokenTimeoutError(Exception):
    pass


def _system_param(blocks: Sequence[SystemBlock]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        block: dict[str, Any] = {"type": "text", "text": b.text}
        if b.cache:
            block["cache_control"] = {"type": "ephemeral"}
        out.append(block)
    return out


def _merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            out[key] = value
    return out


class AnthropicProvider:
    """Direct Anthropic API or Bedrock (Mantle); both clients share the Messages API."""

    def __init__(
        self, name: str, client: anthropic.AsyncAnthropic | AsyncAnthropicBedrockMantle
    ) -> None:
        self.name = name
        self.client = client

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        content: Any = req.content if isinstance(req.content, str) else list(req.content)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": _system_param(req.system),
            "messages": [{"role": "user", "content": content}],
        }
        if req.json_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": dict(req.json_schema)}
            }
        kwargs = _merge(kwargs, params)
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "text" and event.text:
                    yield event.text
            message = await stream.get_final_message()
        u = message.usage
        cached = u.cache_read_input_tokens or 0
        created = u.cache_creation_input_tokens or 0
        yield LLMDone(
            usage=LLMUsage(
                input_tokens=u.input_tokens + cached + created,
                cached_tokens=cached,
                output_tokens=u.output_tokens,
            ),
            stop_reason=message.stop_reason,
            provider=self.name,
            model=model,
        )


type FallbackHook = Callable[[str, Route, str], None]


class ModelRouter:
    def __init__(
        self,
        routes: Mapping[str, Sequence[Route]],
        first_token_timeout_s: float,
        on_fallback: FallbackHook | None = None,
    ) -> None:
        self.routes = routes
        self.first_token_timeout_s = first_token_timeout_s
        self.on_fallback = on_fallback

    async def stream(
        self, job: str, req: LLMRequest, on_fallback: FallbackHook | None = None
    ) -> AsyncGenerator[LLMChunk]:
        routes = self.routes.get(job)
        if not routes:
            raise AllRoutesFailedError(job, ["no routes configured"])
        hooks = [h for h in (self.on_fallback, on_fallback) if h is not None]
        errors: list[str] = []
        for attempt, route in enumerate(routes):
            started = time.monotonic()
            agen = route.provider.stream(route.model, req, route.params)
            try:
                first = await asyncio.wait_for(anext(agen), self.first_token_timeout_s)
            except (TimeoutError, StopAsyncIteration, Exception) as e:  # next route on any error
                await agen.aclose()
                reason = (
                    "first token timeout"
                    if isinstance(e, TimeoutError)
                    else f"{type(e).__name__}: {e}"
                )
                log.warning("%s route %s failed: %s", job, route.label, reason)
                errors.append(f"{route.label}: {reason}")
                for hook in hooks:
                    hook(job, route, reason)
                continue
            first_ms = int((time.monotonic() - started) * 1000)
            try:
                chunk: LLMChunk = first
                while True:
                    if isinstance(chunk, LLMDone):
                        yield LLMDone(
                            usage=chunk.usage,
                            stop_reason=chunk.stop_reason,
                            provider=route.provider.name,
                            model=route.model,
                            attempt=attempt,
                            first_token_ms=first_ms,
                        )
                        return
                    yield chunk
                    chunk = await anext(agen)
            finally:
                await agen.aclose()
        raise AllRoutesFailedError(job, errors)

    async def complete(self, job: str, req: LLMRequest) -> tuple[str, LLMDone]:
        parts: list[str] = []
        done: LLMDone | None = None
        async for chunk in self.stream(job, req):
            if isinstance(chunk, LLMDone):
                done = chunk
            else:
                parts.append(chunk)
        assert done is not None
        return "".join(parts), done
