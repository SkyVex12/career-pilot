"""Model routing layer (ARCHITECTURE.md, "Models and routing" and "Failure modes").

Every model call goes through `ModelRouter`: a job name ("detector", "fast", "deep", ...)
maps to an ordered list of routes. A route is tried until it yields its first token; an error,
or no first token within `first_token_timeout_s` (or the route's own), moves on to the next
route. Once text has streamed, an error is final, since the user has already seen that text.

Refusals: every response is checked for `stop_reason == "refusal"`. A refusal before any
text moves on to the next route that runs a different model, with `between_tools` thinking
removed (only Sonnet 5.5 accepts it); routes marked `refusal_only` are tried only then. This
does on Bedrock what the direct API's server-side `fallbacks` does. A refusal after text has
streamed is passed to the caller as an LLMDone with that stop reason, and `complete()` raises
`RefusedError`, so no caller reads refused content as an answer. When every route refused,
`AllRoutesFailedError.refused` is true, so callers can answer `refused`.

The router is provider-neutral; each provider maps `LLMRequest` to its own wire format and
its responses back to text deltas and one `LLMDone`:
- `AnthropicProvider` wraps the official Anthropic SDK: `AsyncAnthropic` for the direct API
  and `AsyncAnthropicBedrockMantle` for Bedrock. Both expose the same `messages.stream`.
- `OpenAIProvider` wraps the official OpenAI SDK (`AsyncOpenAI`, Responses API). Its refusal
  (a `refusal` output item, or a response stopped by the content filter) becomes
  `stop_reason == "refusal"`, so the router handles it like a Claude refusal.
"""

import asyncio
import base64
import hashlib
import logging
import re
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import openai
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
)

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
    service_tier: str = "standard"  # the tier that served it, from the response's usage


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
    refusal_only: bool = False  # tried only after another model refused
    # This route's first-token timeout, for a model that reasons before its first text;
    # None uses the router's.
    first_token_timeout_s: float | None = None

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.model}"


class AllRoutesFailedError(Exception):
    def __init__(self, job: str, errors: list[str], refused: bool = False) -> None:
        super().__init__(f"{job}: every route failed: {'; '.join(errors)}")
        self.errors = errors
        self.refused = refused  # every route that ran refused the request


class RefusedError(Exception):
    def __init__(self, job: str, done: "LLMDone") -> None:
        super().__init__(f"{job}: refused by {done.provider}:{done.model}")
        self.done = done


_ROUTE_PREFIX = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic\.)?")


def model_family(model: str) -> str:
    """claude-sonnet-5-5, anthropic.claude-sonnet-5-5 and us.anthropic.claude-sonnet-5-5 match;
    dots inside a model name (gpt-6.1-sol) are kept."""
    return _ROUTE_PREFIX.sub("", model).split(":")[0]


def without_between_tools(params: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(params)
    thinking = out.get("thinking")
    if isinstance(thinking, Mapping) and thinking.get("type") == "between_tools":  # pyright: ignore[reportUnknownMemberType]
        del out["thinking"]
    return out


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
            service_tier=str(getattr(u, "service_tier", None) or "standard"),
        )


class OpenAIResponseError(Exception):
    """The Responses API stream reported an error or a failed response."""


# Route params only the OpenAI provider reads: extra output tokens for reasoning on top of the
# request's max_tokens (OpenAI counts reasoning tokens in max_output_tokens).
REASONING_HEADROOM = "max_output_tokens_extra"
_OPENAI_TIERS = {"default": "standard", "auto": "standard"}
# Failed-response codes that are safety stops rather than errors.
POLICY_FAILURES = frozenset({"bio_policy", "misalignment_policy_violation"})


def _openai_document(block: Mapping[str, Any]) -> dict[str, Any]:
    source: Mapping[str, Any] = block["source"]
    title = str(block.get("title") or "document")
    if source.get("type") == "base64":
        media = str(source.get("media_type", "application/pdf"))
        return {
            "type": "input_file",
            "filename": f"{title}.pdf" if media == "application/pdf" else title,
            "file_data": f"data:{media};base64,{source['data']}",
        }
    if source.get("type") == "text":
        body = str(source["data"]).replace("</document>", "</ document>")
        return {"type": "input_text", "text": f'<document title="{title}">\n{body}\n</document>'}
    raise ValueError(f"document source {source.get('type')!r} is not supported on OpenAI")


def openai_content(content: str | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A user turn as Responses API input parts. Content blocks use the Anthropic shapes the
    request builders produce (text, document, image), so each maps to its OpenAI equivalent."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            out.append({"type": "input_text", "text": str(block["text"])})
        elif kind == "document":
            out.append(_openai_document(block))
        elif kind == "image" and block["source"].get("type") == "base64":
            source = block["source"]
            url = f"data:{source['media_type']};base64,{source['data']}"
            out.append({"type": "input_image", "image_url": url, "detail": "auto"})
        else:
            raise ValueError(f"content block {kind!r} is not supported on OpenAI")
    return out


def prompt_cache_key(blocks: Sequence[SystemBlock]) -> str | None:
    """A key for the cacheable prefix (every block up to the last cache breakpoint), so
    requests sharing it reach the same OpenAI cache. A hash: no prompt text leaves in it."""
    last = max((i for i, b in enumerate(blocks) if b.cache), default=-1)
    if last < 0:
        return None
    h = hashlib.sha256()
    for b in blocks[: last + 1]:
        h.update(b.text.encode("utf-8"))
        h.update(b"\0")
    return "pfx-" + base64.urlsafe_b64encode(h.digest()[:18]).decode("ascii")


class OpenAIProvider:
    """OpenAI through the Responses API, streaming.

    The system blocks go first, in order and unchanged, as one developer message, so the
    stable prefix is byte-identical and OpenAI's automatic prompt caching can hit (there is no
    cache_control; a breakpoint only sets `prompt_cache_key`). Requests are not stored
    (`store: false`). A JSON schema becomes a strict structured output.
    """

    def __init__(self, name: str, client: openai.AsyncOpenAI) -> None:
        self.name = name
        self.client = client

    async def warm(self) -> None:
        """Open the connection before a question needs it: the TLS handshake costs as much
        as the first token. A model list is the cheapest authenticated request."""
        await self.client.models.list()

    def request(self, model: str, req: LLMRequest, params: Mapping[str, Any]) -> dict[str, Any]:
        extra = dict(params)
        headroom = int(extra.pop(REASONING_HEADROOM, 0) or 0)
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": b.text} for b in req.system],
                },
                {"role": "user", "content": openai_content(req.content)},
            ],
            "max_output_tokens": req.max_tokens + headroom,
            "store": False,
        }
        if not req.system:
            kwargs["input"] = kwargs["input"][1:]
        if (key := prompt_cache_key(req.system)) is not None:
            kwargs["prompt_cache_key"] = key
        if req.json_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": dict(req.json_schema),
                    "strict": True,
                }
            }
        return _merge(kwargs, extra)

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        kwargs = self.request(model, req, params)
        final: Response | None = None
        refused = False
        stream = await self.client.responses.create(**kwargs, stream=True)
        try:
            async for event in stream:
                if isinstance(event, ResponseTextDeltaEvent):
                    if event.delta:
                        yield event.delta
                elif isinstance(event, ResponseRefusalDeltaEvent):
                    refused = True  # the refusal text is never shown as an answer
                elif isinstance(event, ResponseCompletedEvent | ResponseIncompleteEvent):
                    final = event.response
                elif isinstance(event, ResponseFailedEvent):
                    error = event.response.error
                    code = str(error.code) if error is not None else "unknown"
                    if code in POLICY_FAILURES:
                        # A safety stop: handled like a refusal.
                        refused, final = True, event.response
                        break
                    raise OpenAIResponseError(f"response failed: {code}")
                elif isinstance(event, ResponseErrorEvent):
                    raise OpenAIResponseError(f"stream error: {event.code or 'unknown'}")
        finally:
            await stream.close()
        if final is None:
            raise OpenAIResponseError("stream ended without a final response")
        for item in final.output:
            if isinstance(item, ResponseOutputMessage) and any(
                isinstance(c, ResponseOutputRefusal) for c in item.content
            ):
                refused = True
        stop_reason = "end_turn"
        if refused:
            stop_reason = "refusal"
        elif final.status == "incomplete":
            details = final.incomplete_details
            reason = str(details.reason) if details is not None else "unknown"
            # The content filter is OpenAI's safety stop: handled like a refusal.
            stop_reason = {"max_output_tokens": "max_tokens", "content_filter": "refusal"}.get(
                reason, reason
            )
        u = final.usage
        tier = str(final.service_tier or "default")
        yield LLMDone(
            usage=LLMUsage(
                input_tokens=u.input_tokens if u else 0,  # cached tokens included
                cached_tokens=u.input_tokens_details.cached_tokens if u else 0,
                output_tokens=u.output_tokens if u else 0,  # reasoning tokens included
            ),
            stop_reason=stop_reason,
            provider=self.name,
            model=model,
            service_tier=_OPENAI_TIERS.get(tier, tier),
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
        refused: set[str] = set()  # model families that refused this request
        other_failures = 0

        def failed(route: Route, reason: str) -> None:
            log.warning("%s route %s failed: %s", job, route.label, reason)
            errors.append(f"{route.label}: {reason}")
            for hook in hooks:
                hook(job, route, reason)

        for attempt, route in enumerate(routes):
            if route.refusal_only and not refused:
                continue
            if model_family(route.model) in refused:
                continue
            params = without_between_tools(route.params) if refused else route.params
            started = time.monotonic()
            agen = route.provider.stream(route.model, req, params)
            try:
                timeout = route.first_token_timeout_s or self.first_token_timeout_s
                first = await asyncio.wait_for(anext(agen), timeout)
            except (TimeoutError, StopAsyncIteration, Exception) as e:  # next route on any error
                await agen.aclose()
                other_failures += 1
                failed(
                    route,
                    "first token timeout"
                    if isinstance(e, TimeoutError)
                    else f"{type(e).__name__}: {e}",
                )
                continue
            if isinstance(first, LLMDone) and first.stop_reason == "refusal":
                await agen.aclose()
                refused.add(model_family(route.model))
                failed(route, "refusal")
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
                            service_tier=chunk.service_tier,
                        )
                        return
                    yield chunk
                    chunk = await anext(agen)
            finally:
                await agen.aclose()
        raise AllRoutesFailedError(job, errors, refused=bool(refused) and not other_failures)

    async def warm(self) -> None:
        """Open each provider's connection ahead of the first question, where the provider
        supports it (OpenAIProvider). Failures only cost the head start."""
        providers = {id(r.provider): r.provider for rs in self.routes.values() for r in rs}
        for provider in providers.values():
            warm = getattr(provider, "warm", None)
            if warm is None:
                continue
            try:
                await warm()
            except Exception as e:
                log.info("warming %s failed: %s", provider.name, type(e).__name__)

    async def complete(self, job: str, req: LLMRequest) -> tuple[str, LLMDone]:
        parts: list[str] = []
        done: LLMDone | None = None
        async for chunk in self.stream(job, req):
            if isinstance(chunk, LLMDone):
                done = chunk
            else:
                parts.append(chunk)
        assert done is not None
        if done.stop_reason == "refusal":
            raise RefusedError(job, done)
        return "".join(parts), done
