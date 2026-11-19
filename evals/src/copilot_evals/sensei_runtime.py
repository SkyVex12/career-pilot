"""Private benchmark preparation and timed gateway replay; never print payloads.

Only the storage and speech-to-text transports are local doubles. Worker extraction,
pack building, gateway detection, answers, retrieval and conversation use shipped code.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from copilot_core import keys
from copilot_core.llm import Decided, LLMDone, LLMRequest
from copilot_core.pricing import ModelPrice, cost_usd, model_key

UID = uuid5(NAMESPACE_URL, "sensei-benchmark/sergio")
APP = uuid5(UID, "turncare")
RESUME = uuid5(APP, "supplied-resume")


def save(path: Path, data: Any) -> None:
    """Atomic private checkpoint, never a console dump."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def private_directory(path: Path) -> Path:
    path = path.resolve()
    repo = Path(__file__).resolve().parents[3]
    if path == repo or repo in path.parents:
        raise ValueError("Private artifacts must be outside the repository")
    path.mkdir(parents=True, exist_ok=True)
    return path


class Budget:
    """Reserve before dispatch, including hedges; unknown usage keeps its reservation.

    UTF-8 byte count plus schema/transport allowance bounds text input tokens.
    Unknown/cancelled calls are conservatively charged their entire reservation.
    One ledger is shared by preparation, replay and judging; run these sequentially.
    """

    def __init__(self, directory: Path, settings: Any, limit: float | None = None):
        self.path = directory / "cost.json"
        self.settings = settings
        settings.model_prices.setdefault(
            "text-embedding-3-large", ModelPrice(input=0.13, cached=0.13, output=0)
        )
        self.limit = (
            limit
            if limit is not None
            else float(json.loads((directory / "budget-limit.json").read_text("utf-8")))
            if (directory / "budget-limit.json").exists()
            else 2.95
        )
        self.entries: list[dict[str, Any]] = (
            json.loads(self.path.read_text("utf-8")) if self.path.exists() else []
        )

    @property
    def total(self) -> float:
        return sum(float(e["charged_usd"]) for e in self.entries)

    def reserve(self, model: str, payload: Any, output: int, tier: str = "standard") -> int:
        prices = self.settings.model_prices
        if model_key(model) not in prices:
            raise ValueError("Model price missing; refusing billed call")
        tokens = len(json.dumps(payload, ensure_ascii=False, default=str).encode()) + 1024
        value = cost_usd(
            model, tier, tokens, 0, output, prices, self.settings.tier_price_multipliers
        )
        if value is None:
            raise ValueError("Tier price missing; refusing billed call")
        upper = float(value) * (1.25 if model.startswith("claude") else 1.0)
        if self.total + upper > self.limit:
            raise ValueError("Benchmark budget exhausted before dispatch")
        self.entries.append({"model": model, "charged_usd": upper, "status": "reserved"})
        save(self.path, self.entries)
        return len(self.entries) - 1

    def finish(self, ticket: int, done: LLMDone) -> None:
        u = done.usage
        value = cost_usd(
            done.model,
            done.service_tier,
            u.input_tokens,
            u.cached_tokens,
            u.output_tokens,
            self.settings.model_prices,
            self.settings.tier_price_multipliers,
        )
        if value is None:
            return
        self.entries[ticket].update(
            charged_usd=float(value), status="usage", usage=asdict(u), tier=done.service_tier
        )
        save(self.path, self.entries)


class MeteredProvider:
    def __init__(self, provider: Any, budget: Budget):
        self.inner = provider
        self.name: str = provider.name
        self.budget = budget

    def connection_failure(self, ticket: int, error: BaseException) -> None:
        # A failed connection/pool acquisition cannot have submitted a model request.
        causes: list[str] = []
        current: BaseException | None = error
        while current is not None:
            causes.append(type(current).__name__)
            current = current.__cause__
        if any(name in ("ConnectTimeout", "ConnectError", "PoolTimeout") for name in causes):
            self.budget.entries[ticket].update(
                charged_usd=0.0, status="failed_before_submission", error_types=causes
            )
            save(self.budget.path, self.budget.entries)

    async def warm(self, model: str) -> None:
        await self.inner.warm(model)

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        ticket = self.budget.reserve(
            model,
            asdict(req),
            req.max_tokens + int(params.get("max_output_tokens_extra", 0)),
            str(params.get("service_tier", "standard")),
        )
        try:
            async for chunk in self.inner.stream(model, req, params):
                if isinstance(chunk, LLMDone):
                    self.budget.finish(ticket, chunk)
                yield chunk
        except Exception as error:
            self.connection_failure(ticket, error)
            raise

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> LLMDone | None:
        ticket = self.budget.reserve(model, asdict(req), 0)
        try:
            done: LLMDone | None = await self.inner.prewarm(model, req, params)
        except Exception as error:
            self.connection_failure(ticket, error)
            raise
        if done is not None:
            self.budget.finish(ticket, done)
        return done

    async def decide(
        self, model: str, input: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]]
    ) -> Decided:
        ticket = self.budget.reserve(model, [input, questions], 0)
        try:
            done: Decided = await self.inner.decide(model, input, questions)
        except Exception as error:
            self.connection_failure(ticket, error)
            raise
        self.budget.finish(ticket, LLMDone(done.usage, "decided", self.name, model))
        return done


class MeteredEmbedder:
    def __init__(self, inner: Any, budget: Budget):
        self.inner = inner
        self.model_id: str = inner.model_id
        self.budget = budget

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Reserve a byte-bound at the embedding list price; the adapter does not expose usage.
        self.budget.reserve(self.inner.model, texts, 0)
        return cast(list[list[float]], await self.inner.embed(texts))


class MemoryConnection:
    """Minimal in-memory storage adapter for the unchanged worker builders."""

    def __init__(self, jd: str):
        self.jd = jd
        self.nodes: list[dict[str, Any]] = []

    async def execute(self, query: str, *args: Any) -> None:
        if not query.startswith("update app.memory_nodes"):
            raise ValueError("Unexpected preparation write")

    async def executemany(self, query: str, rows: Any) -> None:
        if query.startswith("insert into app.memory_nodes"):
            for row in rows:
                self.nodes.append(
                    dict(
                        zip(
                            (
                                "id",
                                "user_id",
                                "kind",
                                "title",
                                "summary",
                                "body",
                                "source",
                                "resume_version_id",
                            ),
                            row,
                            strict=True,
                        )
                    )
                )
        elif not query.startswith("insert into app.memory_edges"):
            raise ValueError("Unexpected preparation write")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "app.paired_resume" in query:
            return {
                "id": APP,
                "rv": RESUME,
                "kind": "base",
                "manual": True,
                "jd_text": self.jd,
                "pairing_rev": 1,
            }
        if "from app.applications" in query:
            return {
                "job_id": APP,
                "interview_id": None,
                "round": None,
                "company": "TurnCare",
                "role": "Full Stack Developer",
                "jd_text": self.jd,
            }
        raise ValueError("Unexpected preparation read")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "from app.memory_nodes" not in query:
            raise ValueError("Unexpected preparation read")
        return [n for n in self.nodes if n["kind"] in ("role", "project", "story", "skill")]


async def prepare(directory: Path, resume: str, jd: str) -> None:
    from fakeredis import FakeAsyncValkey

    from copilot_core.embed import make_embedder
    from copilot_core.providers import make_providers
    from copilot_worker import prompts
    from copilot_worker.config import WorkerSettings
    from copilot_worker.llm import build_router, complete_json
    from copilot_worker.memory import render_digest, replace_resume_nodes
    from copilot_worker.packs import build_interview_pack

    # The local extract route has no published price in the configured price table.
    # Use the priced pack model for this newly created benchmark profile only.
    settings = WorkerSettings(openai_extract_model="gpt-6.1-sol", llm_first_token_timeout_s=120)
    budget = Budget(directory, settings)
    providers = [MeteredProvider(p, budget) for p in make_providers(settings, timeout_s=120)]
    router = build_router(settings, providers)
    conn = MemoryConnection(jd)
    valkey = FakeAsyncValkey()

    @contextlib.asynccontextmanager
    async def user(uid: UUID) -> AsyncGenerator[Any]:
        yield conn

    extracted = directory / "extracted.json"
    if extracted.exists():
        data = json.loads(extracted.read_text("utf-8"))
    else:
        data = await complete_json(
            router,
            "extract",
            prompts.resume_request({"type": "text", "text": "<resume>\n" + resume + "\n</resume>"}),
        )
        save(extracted, data)
    await replace_resume_nodes(cast(Any, conn), UID, RESUME, data)
    digest = render_digest("Sergio", conn.nodes)
    if digest is None:
        raise ValueError("Resume produced no career facts")
    await valkey.set(keys.resume_digest(UID, RESUME), digest.model_dump_json(by_alias=True))
    embedder = make_embedder(settings)
    ctx = SimpleNamespace(
        settings=settings,
        user=user,
        valkey=valkey,
        router=router,
        embedder=MeteredEmbedder(embedder, budget) if embedder else None,
    )
    result = await build_interview_pack(cast(Any, ctx), UID, f"job:{APP}")
    if result is None:
        raise ValueError("No pack was built")
    pack = result[0]
    save(
        directory / "profile.json",
        {
            "digest": digest.model_dump(by_alias=True),
            "pack": pack.model_dump(by_alias=True),
            "nodes": conn.nodes,
            "resume_source": resume,
            "jd_source": jd,
        },
    )
    await valkey.aclose()


async def replay(directory: Path, turns: list[dict[str, Any]], *, speed: float = 1.0) -> None:
    from fakeredis import FakeAsyncValkey

    from copilot_core.embed import make_embedder
    from copilot_core.jobs import MemoryJobQueue
    from copilot_core.packs import Digest, SessionPack
    from copilot_core.providers import make_providers
    from copilot_gateway.auth import Principal, StaticVerifier
    from copilot_gateway.config import Settings
    from copilot_gateway.router import build_router
    from copilot_gateway.session import Services
    from copilot_gateway.store import GatewayStore
    from copilot_gateway.testing import GatewayServer, ProtocolClient, TextSTT, TextSTTControl

    if speed <= 0:
        raise ValueError("Speed must be positive")
    profile = json.loads((directory / "profile.json").read_text("utf-8"))
    digest, pack = (
        Digest.model_validate(profile["digest"]),
        SessionPack.model_validate(profile["pack"]),
    )
    settings = Settings()
    budget = Budget(directory, settings)
    providers = [MeteredProvider(p, budget) for p in make_providers(settings)]
    valkey = FakeAsyncValkey()
    await valkey.set(keys.resume_digest(UID, RESUME), digest.model_dump_json(by_alias=True))
    await valkey.set(keys.pack(UID, pack.pack_id), pack.model_dump_json(by_alias=True))
    await cast(Any, valkey).hset(keys.pack_index(UID), mapping={f"job:{APP}": str(pack.pack_id)})
    embedder = make_embedder(settings)
    control = TextSTTControl()
    services = Services(
        settings=settings,
        store=GatewayStore(valkey, settings),
        jobs=MemoryJobQueue(),
        router=build_router(settings, providers),
        verifier=StaticVerifier({"token-a": Principal(UID, "Sergio", None)}),
        stt_providers=[("text", lambda terms: TextSTT(control))],
        embedder=MeteredEmbedder(embedder, budget) if embedder else None,
    )
    events: list[dict[str, Any]] = []
    t0 = time.monotonic()

    class TimedMessages(list[dict[str, Any]]):
        def append(self, message: dict[str, Any]) -> None:
            events.append({"at": time.monotonic() - t0, "message": message})
            super().append(message)

    async with GatewayServer(services) as gateway:
        client = ProtocolClient(gateway.url)
        client.messages = TimedMessages()
        await client.start(job_id=str(APP), consent={"store_transcript": False})
        session = next(iter(gateway.manager.sessions.values()))
        if session.pack is None or session.digest is None:
            raise ValueError("Replay did not load the paired resume and pack")
        t0 = time.monotonic()
        sent: list[dict[str, Any]] = []
        for turn in turns:
            delay = float(turn["offset_s"]) / speed - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            if client.close_code is not None:
                raise ValueError("Gateway closed before replay completed")
            at = time.monotonic() - t0
            # Only question text enters the gateway. Suggestions never enter its context.
            await client.say(str(turn["question"]), capture_ms=int(at * 1000))
            sent.append({"id": turn["id"], "at": at})
            save(
                directory / "progress.json",
                {
                    "sent": len(sent),
                    "total": len(turns),
                    "elapsed_s": at,
                    "cost_upper_usd": budget.total,
                },
            )
            save(directory / "replay.json", {"speed": speed, "sent": sent, "events": events})
        await asyncio.sleep(20)
        await client.send({"type": "session.end"})
        await client.closed(timeout=15)
        logs = await valkey.xrange(keys.sess_log(client.session_id))
        decoded = [json.loads(fields[b"e"]) for _, fields in logs if b"e" in fields]
        save(
            directory / "replay.json",
            {
                "speed": speed,
                "sent": sent,
                "events": events,
                "metadata_logs": decoded,
                "complete": True,
                "cost_upper_usd": budget.total,
            },
        )
    await valkey.aclose()
