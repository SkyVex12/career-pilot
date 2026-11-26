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
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic
import openai
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
from anthropic.types import Message
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

from copilot_core.pricing import openai_cache_write_pricing

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
    # OpenAI GPT-5.6+ reports writes separately from reads. None means unobserved,
    # not zero; older SDKs retain the TTL breakdown as extra response fields.
    cache_write_tokens: int | None = None
    cache_write_30m_tokens: int | None = None
    cache_write_12h_tokens: int | None = None
    reasoning_tokens: int | None = None  # already included in output_tokens; never add to cost


@dataclass(frozen=True)
class LLMDone:
    usage: LLMUsage
    stop_reason: str | None
    provider: str = ""
    model: str = ""
    attempt: int = 0  # index of the route that answered; above 0 means a fallback ran
    first_token_ms: int = 0
    service_tier: str = "standard"  # the tier that served it, from the response's usage
    hedged: bool = False  # a second route ran alongside (ModelRouter.hedge_after_s)


type LLMChunk = str | LLMDone


@dataclass(frozen=True)
class Decided:
    """A Decisions API response: its answers as dicts (each with `type`, `name` and, for a
    choice, `choice`, `confidence` and `probabilities`) and its usage."""

    answers: list[dict[str, Any]]
    usage: LLMUsage
    provider: str = ""
    model: str = ""


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

    async def warm(self, model: str) -> None:
        """Open the connection before a question needs it: a cold connection to the Claude API
        took 4.6 s in October 2026 against 0.45 s warm. The direct API lists models; Bedrock
        Mantle has no models endpoint, so it counts the tokens of a one-word message, which
        costs nothing."""
        if isinstance(self.client, anthropic.AsyncAnthropic):
            await self.client.models.list(limit=1)
        else:
            await self.client.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": "warm-up"}]
            )

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> "LLMDone | None":
        """Write the prompt cache for `req`'s system blocks before the first real request: a
        `max_tokens: 0` request runs prefill only (cache write at the breakpoints, no output),
        with the route's own settings so the entry matches real traffic. The user turn is a
        placeholder, after the last breakpoint."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 0,
            "system": _system_param(req.system),
            "messages": [{"role": "user", "content": "warm-up"}],
        }
        message = cast(Message, await self.client.messages.create(**_merge(kwargs, params)))
        u = message.usage
        cached = u.cache_read_input_tokens or 0
        created = u.cache_creation_input_tokens or 0
        return LLMDone(
            usage=LLMUsage(input_tokens=u.input_tokens + cached + created, cached_tokens=cached),
            stop_reason=message.stop_reason,
            provider=self.name,
            model=model,
            service_tier=str(getattr(u, "service_tier", None) or "standard"),
        )

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
            # "high": "auto" may read a screenshot at low resolution, where code is unreadable.
            out.append({"type": "input_image", "image_url": url, "detail": "high"})
        else:
            raise ValueError(f"content block {kind!r} is not supported on OpenAI")
    return out


def supports_cache_options(model: str) -> bool:
    """`prompt_cache_breakpoint` and `prompt_cache_options` exist from GPT-5.6 on; older models
    reject both with a 400 (checked October 2026: gpt-5.4-mini rejects them, gpt-6-luna and
    gpt-6.1-sol accept them)."""
    return openai_cache_write_pricing(model)


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


def _openai_usage(raw: Any, *, include_output: bool = True) -> LLMUsage:
    """Read SDK objects or raw usage without inventing cache-write counters.

    SDK 3.26 exposes aggregate writes as a typed field and retains the documented
    TTL breakdown as an extra dict. A future typed breakdown works the same way.
    Request TTL/defaults are deliberately not used to fill absent response usage.
    """

    def value(obj: Any, name: str) -> Any:
        return (
            cast(Mapping[str, Any], obj).get(name)
            if isinstance(obj, Mapping)
            else getattr(obj, name, None)
        )

    def counter(obj: Any, name: str) -> int | None:
        observed = value(obj, name)
        return observed if type(observed) is int and observed >= 0 else None

    details = value(raw, "input_tokens_details")
    writes = value(details, "cache_write_tokens_details")
    return LLMUsage(
        input_tokens=counter(raw, "input_tokens") or 0,
        cached_tokens=counter(details, "cached_tokens") or 0,
        output_tokens=(counter(raw, "output_tokens") or 0) if include_output else 0,
        cache_write_tokens=counter(details, "cache_write_tokens"),
        cache_write_30m_tokens=counter(writes, "ttl_30m_tokens"),
        cache_write_12h_tokens=counter(writes, "ttl_12h_tokens"),
        reasoning_tokens=counter(value(raw, "output_tokens_details"), "reasoning_tokens"),
    )


class OpenAIProvider:
    """OpenAI through the Responses API, streaming.

    The system blocks go first, in order and unchanged, as one developer message, so the
    stable prefix is byte-identical. GPT-5.6+ uses explicit breakpoints; earlier models
    use automatic prefix caching with `prompt_cache_key`. Requests are not stored
    (`store: false`). A JSON schema becomes a strict structured output.
    """

    def __init__(self, name: str, client: openai.AsyncOpenAI) -> None:
        self.name = name
        self.client = client

    async def warm(self, model: str) -> None:
        """Open the connection before a question needs it: the TLS handshake costs as much
        as the first token. A model list is the cheapest authenticated request."""
        await self.client.models.list()

    def request(self, model: str, req: LLMRequest, params: Mapping[str, Any]) -> dict[str, Any]:
        extra = dict(params)
        headroom = int(extra.pop(REASONING_HEADROOM, 0) or 0)
        # From GPT-5.6 on, the cache is written at breakpoints: one after each block marked for
        # caching (as Claude's cache_control), in explicit mode, so each request's unique tail
        # (transcript and question) is not written as a cache entry too.
        breakpoints = supports_cache_options(model) and any(b.cache for b in req.system)
        developer: list[dict[str, Any]] = []
        for b in req.system:
            part: dict[str, Any] = {"type": "input_text", "text": b.text}
            if breakpoints and b.cache:
                part["prompt_cache_breakpoint"] = {"mode": "explicit"}
            developer.append(part)
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "developer", "content": developer},
                {"role": "user", "content": openai_content(req.content)},
            ],
            "max_output_tokens": req.max_tokens + headroom,
            "store": False,
        }
        if not req.system:
            kwargs["input"] = kwargs["input"][1:]
        if (key := prompt_cache_key(req.system)) is not None:
            kwargs["prompt_cache_key"] = key
        if breakpoints:
            kwargs["extra_body"] = {"prompt_cache_options": {"mode": "explicit"}}
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

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> "LLMDone | None":
        """Write the prompt cache for `req`'s system blocks without generating output
        (`prompt_cache_options.prewarm`, GPT-5.6 and later; None on older models, which lack
        it). Writes are billed at the rate for their reported cache lifetime."""
        if not supports_cache_options(model) or not any(b.cache for b in req.system):
            return None
        kwargs = self.request(model, req, params)
        kwargs["extra_body"] = {"prompt_cache_options": {"mode": "explicit", "prewarm": True}}
        response = cast(Response, await self.client.responses.create(**kwargs))
        u = response.usage
        tier = str(response.service_tier or "default")
        return LLMDone(
            usage=_openai_usage(u),
            stop_reason="prewarm",
            provider=self.name,
            model=model,
            service_tier=_OPENAI_TIERS.get(tier, tier),
        )

    async def decide(
        self,
        model: str,
        input: Sequence[Mapping[str, Any]],
        questions: Sequence[Mapping[str, Any]],
    ) -> "Decided":
        """One call to the Decisions API (POST /v1/decisions): typed answers to `questions`
        about `input`, not generated text. Only input tokens are billed; the output count is
        reported as 0 so cost comes from the input alone."""
        d = await self.client.decisions.create(
            model=model,
            input=cast(Any, list(input)),
            questions=cast(Any, list(questions)),
        )
        u = d.usage
        usage = _openai_usage(u, include_output=False)
        return Decided([a.model_dump() for a in d.answers], usage, self.name, model)

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
            usage=_openai_usage(u),
            stop_reason=stop_reason,
            provider=self.name,
            model=model,
            service_tier=_OPENAI_TIERS.get(tier, tier),
        )


type FallbackHook = Callable[[str, Route, str], None]


def error_chain(error: BaseException) -> tuple[str, ...]:
    """Exception class names only: SDK error text can contain request data or credentials."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return tuple(names)


def error_category(error: BaseException) -> str:
    names = error_chain(error)
    for name, category in (
        ("ConnectTimeout", "connect_timeout"),
        ("ConnectError", "connect_error"),
        ("PoolTimeout", "pool_timeout"),
        ("ReadTimeout", "read_timeout"),
        ("WriteTimeout", "write_timeout"),
    ):
        if name in names:
            return category
    status = getattr(error, "status_code", None)
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "authentication"
    if isinstance(status, int):
        return "http_error"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if "APITimeoutError" in names:
        return "provider_timeout"
    if "APIConnectionError" in names:
        return "connection_error"
    return "provider_error"


@dataclass(frozen=True)
class AttemptEvent:
    """One provider invocation's lifecycle, without prompt text or guessed usage.

    A completed response is recorded before yielding LLMDone, so a consumer closing at
    that point is not mistaken for a failed request. Canceled requests without usage
    retain unknown billing exposure, including the losing half of a hedge.
    """

    request_id: str
    attempt_id: str
    job: str
    attempt: int
    provider: str
    model: str
    status: str
    stage: str
    elapsed_ms: int
    since_request_ms: int
    error_type: str | None = None
    cause_types: tuple[str, ...] = ()
    http_status: int | None = None
    error_category: str | None = None
    usage: LLMUsage | None = None
    service_tier: str | None = None


type AttemptHook = Callable[[AttemptEvent], None]


@dataclass
class _Attempt:
    request_id: str
    job: str
    index: int
    route: Route
    request_started: float
    hooks: list[AttemptHook]
    started: float = field(default_factory=time.monotonic)
    agen: AsyncGenerator[LLMChunk] | None = None
    received_first: bool = False
    terminal: bool = False
    closed: bool = False
    close_reason: str = "caller_cancelled"

    def emit(
        self,
        status: str,
        *,
        error: BaseException | None = None,
        category: str | None = None,
        done: LLMDone | None = None,
    ) -> None:
        if self.terminal:
            return
        if status in {"completed", "failed", "cancelled", "closed"}:
            self.terminal = True
        now = time.monotonic()
        code = getattr(error, "status_code", None)
        event = AttemptEvent(
            request_id=self.request_id,
            attempt_id=f"{self.request_id}:{self.index}",
            job=self.job,
            attempt=self.index,
            provider=self.route.provider.name,
            model=self.route.model,
            status=status,
            stage="stream" if self.received_first else "first_chunk",
            elapsed_ms=int((now - self.started) * 1000),
            since_request_ms=int((now - self.request_started) * 1000),
            error_type=type(error).__name__ if error else None,
            cause_types=error_chain(error) if error else (),
            http_status=code if isinstance(code, int) else None,
            error_category=category or (error_category(error) if error else None),
            usage=done.usage if done else None,
            service_tier=done.service_tier if done else None,
        )
        for hook in self.hooks:
            try:
                hook(event)
            except Exception:
                # Observability must not turn a successful provider call into a retry.
                log.exception("provider attempt observer failed")

    async def close(self, reason: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.emit("closed", category=reason or self.close_reason)
        if self.agen is not None:
            await self.agen.aclose()


class ModelRouter:
    def __init__(
        self,
        routes: Mapping[str, Sequence[Route]],
        first_token_timeout_s: float,
        on_fallback: FallbackHook | None = None,
        hedge_after_s: Mapping[str, float] | None = None,
        on_attempt: AttemptHook | None = None,
    ) -> None:
        self.routes = routes
        self.first_token_timeout_s = first_token_timeout_s
        self.on_fallback = on_fallback
        self.on_attempt = on_attempt
        # Per job: when the first route has no first token after this long, the next route
        # starts alongside it and whichever answers first is used (a hedged request), instead
        # of waiting out the first-token timeout before retrying.
        self.hedge_after_s = dict(hedge_after_s or {})
        self.hedges = 0

    async def _open(
        self, route: Route, req: LLMRequest, params: Mapping[str, Any], state: _Attempt
    ) -> tuple[AsyncGenerator[LLMChunk], LLMChunk, int]:
        """A route's stream and its first chunk, within the route's first-token timeout."""
        started = time.monotonic()
        agen = route.provider.stream(route.model, req, params)
        state.agen = agen
        state.emit("started")
        first_task = asyncio.ensure_future(anext(agen))
        try:
            timeout = route.first_token_timeout_s or self.first_token_timeout_s
            first = await asyncio.wait_for(first_task, timeout)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                state.emit("cancelled", error=error, category=state.close_reason)
            else:
                state.emit(
                    "failed",
                    error=error,
                    category=(
                        "first_token_timeout"
                        if isinstance(error, TimeoutError) and first_task.cancelled()
                        else None
                    ),
                )
            await state.close()
            raise
        state.emit("first_chunk")
        state.received_first = True
        if isinstance(first, LLMDone):
            state.emit("completed", done=first)
        return agen, first, int((time.monotonic() - started) * 1000)

    async def stream(
        self,
        job: str,
        req: LLMRequest,
        on_fallback: FallbackHook | None = None,
        *,
        on_attempt: AttemptHook | None = None,
    ) -> AsyncGenerator[LLMChunk]:
        attempts: list[_Attempt] = []
        inner = self._stream(job, req, on_fallback, on_attempt, attempts)
        try:
            async for chunk in inner:
                yield chunk
        finally:
            try:
                await inner.aclose()
            finally:
                # Includes a winning stream canceled while a losing hedge is closing.
                for attempt in attempts:
                    await attempt.close()

    async def _stream(
        self,
        job: str,
        req: LLMRequest,
        on_fallback: FallbackHook | None,
        on_attempt: AttemptHook | None,
        attempts: list[_Attempt],
    ) -> AsyncGenerator[LLMChunk]:
        request_id, request_started = uuid.uuid4().hex, time.monotonic()
        attempt_hooks = [h for h in (self.on_attempt, on_attempt) if h is not None]
        routes = self.routes.get(job)
        if not routes:
            raise AllRoutesFailedError(job, ["no routes configured"])
        hooks = [h for h in (self.on_fallback, on_fallback) if h is not None]
        errors: list[str] = []
        refused: set[str] = set()  # model families that refused this request
        other_failures = 0
        hedge_after = self.hedge_after_s.get(job)

        def failed(route: Route, reason: str) -> None:
            log.warning("%s route %s failed: %s", job, route.label, reason)
            errors.append(f"{route.label}: {reason}")
            for hook in hooks:
                hook(job, route, reason)

        def eligible(route: Route) -> bool:
            if route.refusal_only and not refused:
                return False
            return model_family(route.model) not in refused

        def start(
            index: int, route: Route
        ) -> tuple[asyncio.Task[tuple[AsyncGenerator[LLMChunk], LLMChunk, int]], _Attempt]:
            params = without_between_tools(route.params) if refused else route.params
            state = _Attempt(request_id, job, index, route, request_started, attempt_hooks)
            attempts.append(state)
            return asyncio.ensure_future(self._open(route, req, params, state)), state

        pending = list(enumerate(routes))
        while pending:
            attempt, route = pending.pop(0)
            if not eligible(route):
                continue
            task, state = start(attempt, route)
            racing = {task: (attempt, route, state)}
            won: tuple[int, Route, AsyncGenerator[LLMChunk], LLMChunk, int, _Attempt] | None = None
            hedged = False
            try:
                if hedge_after is not None:
                    await asyncio.wait(racing, timeout=hedge_after)
                    nxt = next((p for p in pending if eligible(p[1])), None)
                    if nxt is not None and not any(t.done() for t in racing):
                        pending.remove(nxt)
                        self.hedges += 1
                        hedged = True
                        log.info("%s: no first token after %.1f s, hedging", job, hedge_after)
                        task, state = start(*nxt)
                        racing[task] = (*nxt, state)
                while racing and won is None:
                    done, _ = await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        a, r, state = racing.pop(task)
                        try:
                            agen, first, first_ms = task.result()
                        except Exception as e:  # next route on any error
                            other_failures += 1
                            timed_out = isinstance(e, TimeoutError)
                            failed(
                                r,
                                "first token timeout" if timed_out else f"{type(e).__name__}: {e}",
                            )
                            continue
                        if isinstance(first, LLMDone) and first.stop_reason == "refusal":
                            await state.close()
                            refused.add(model_family(r.model))
                            failed(r, "refusal")
                            continue
                        if won is None:
                            won = (a, r, agen, first, first_ms, state)
                        else:
                            await state.close("hedge_lost")
            finally:
                for task, (_, _, state) in racing.items():
                    state.close_reason = "hedge_lost" if won is not None else "caller_cancelled"
                    task.cancel()
                await asyncio.gather(*racing, return_exceptions=True)
                for _, _, state in racing.values():
                    await state.close()
            if won is None:
                continue
            attempt, route, agen, first, first_ms, state = won
            try:
                chunk: LLMChunk = first
                while True:
                    if isinstance(chunk, LLMDone):
                        state.emit("completed", done=chunk)
                        yield LLMDone(
                            usage=chunk.usage,
                            stop_reason=chunk.stop_reason,
                            provider=route.provider.name,
                            model=route.model,
                            attempt=attempt,
                            first_token_ms=first_ms,
                            service_tier=chunk.service_tier,
                            hedged=hedged,
                        )
                        return
                    yield chunk
                    chunk = await anext(agen)
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    state.emit("cancelled", error=error, category="caller_cancelled")
                elif isinstance(error, GeneratorExit):
                    state.emit("closed", category="consumer_closed")
                else:
                    state.emit("failed", error=error)
                raise
            finally:
                await state.close()
        raise AllRoutesFailedError(job, errors, refused=bool(refused) and not other_failures)

    async def warm(self) -> None:
        """Open each provider's connection ahead of the first question (every provider here
        has `warm(model)`, given the model of its first route). Failures only cost the head
        start."""
        first: dict[int, Route] = {}
        for routes in self.routes.values():
            for r in routes:
                first.setdefault(id(r.provider), r)
        for route in first.values():
            warm = getattr(route.provider, "warm", None)
            if warm is None:
                continue
            try:
                await warm(route.model)
            except Exception as e:
                log.info("warming %s failed: %s", route.provider.name, type(e).__name__)

    async def prewarm(self, job: str, req: LLMRequest) -> LLMDone | None:
        """Write the prompt cache for a job's first route, where its provider and model support
        it, so the first real request is not cold. None when nothing was written; failures
        only cost the head start."""
        route = next((r for r in self.routes.get(job, ()) if not r.refusal_only), None)
        prewarm = getattr(route.provider, "prewarm", None) if route is not None else None
        if route is None or prewarm is None:
            return None
        try:
            done: LLMDone | None = await prewarm(route.model, req, route.params)
        except Exception as e:
            log.info("prewarming %s failed: %s: %s", route.label, type(e).__name__, e)
            return None
        return done

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
