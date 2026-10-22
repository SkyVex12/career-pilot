"""Live memory search for standups and meetings at a company (ARCHITECTURE.md, "Realtime
pipeline" step 4 and "Hybrid memory search").

Meeting questions cannot all be prepared ahead, so each question searches the chosen
company's memory while the detector runs. The gateway connects as `app_gateway` (read only,
under row-level security) to Postgres on the same host, and every query is scoped to the
session's user and employer:

- Full text starts at once on the question's text (a few ms locally). The fast pass waits for
  it at most `memory_fast_wait_ms` and gets what came back as a small `<memory>` block.
- The vector half waits for the question's embedding, which prepared-answer matching makes
  anyway (shared, one embedding call per text), then the hybrid query runs. The deep pass,
  which starts at confirmation, waits at most `memory_deep_wait_ms` for it, and otherwise takes
  the full-text results.

Each query has the `retrieval_budget_ms` budget; past it, or on any error, the pass goes on
without memory (ARCHITECTURE.md, "Failure modes": Aurora slow or down).
"""

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from copilot_core.db import user_tx
from copilot_core.workplace import Hit, search

log = logging.getLogger(__name__)

type VectorSource = Callable[[], Awaitable[list[float] | None]]


@dataclass
class Lookup:
    """One text's search: full text first, then the hybrid result."""

    text: str
    started: float
    fts: "asyncio.Task[list[Hit]]"
    full: "asyncio.Task[list[Hit]]"
    ms: dict[str, int] = field(default_factory=dict[str, int])

    def cancel(self) -> None:
        self.fts.cancel()
        self.full.cancel()


class CompanyMemory:
    """Search over one company's memory for one session."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        user_id: UUID,
        employer_id: UUID,
        *,
        embedding_model: str | None,
        budget_ms: int,
        limit: int,
        cache_size: int = 32,
    ) -> None:
        self.pool = pool
        self.user_id = user_id
        self.employer_id = employer_id
        self.embedding_model = embedding_model
        self.budget_s = budget_ms / 1000
        self.limit = limit
        self.cache_size = cache_size
        self.lookups: OrderedDict[str, Lookup] = OrderedDict()
        self.failures = 0

    def lookup(self, text: str, vector: VectorSource | None) -> Lookup:
        """The search for this text, started now or earlier (the turn starts it with the
        detector; a pass asks again by the same text)."""
        found = self.lookups.get(text)
        if found is not None:
            self.lookups.move_to_end(text)
            return found
        started = time.monotonic()
        fts = asyncio.create_task(self._query(text, None, "memory_fts_ms", started))

        async def full() -> list[Hit]:
            vec = None
            if vector is not None and self.embedding_model:
                with contextlib.suppress(Exception):
                    vec = await vector()
            if vec is None:
                return await fts
            return await self._query(text, vec, "memory_hybrid_ms", started)

        item = Lookup(text, started, fts, asyncio.create_task(full()))
        self.lookups[text] = item
        while len(self.lookups) > self.cache_size:
            _, old = self.lookups.popitem(last=False)
            old.cancel()
        return item

    async def _query(
        self, text: str, vector: list[float] | None, name: str, started: float
    ) -> list[Hit]:
        t = time.monotonic()
        try:
            async with asyncio.timeout(self.budget_s):
                async with user_tx(self.pool, self.user_id) as conn:
                    hits = await search(
                        conn, self.user_id, self.employer_id, text, vector,
                        self.embedding_model, limit=self.limit,
                    )  # fmt: skip
        except TimeoutError:
            self.failures += 1
            log.warning("memory search over its %d ms budget", int(self.budget_s * 1000))
            return []
        except (OSError, asyncpg.PostgresError) as e:
            self.failures += 1
            log.warning("memory search failed: %s", e)
            return []
        lookup = self.lookups.get(text)
        if lookup is not None:
            lookup.ms[name] = int((time.monotonic() - t) * 1000)
        return hits

    async def hits(self, text: str, vector: VectorSource | None, deep: bool,
                   wait_ms: int) -> tuple[list[Hit], dict[str, int]]:  # fmt: skip
        """What the search has for a pass within `wait_ms` from now: the hybrid result for
        the deep pass if it is in, else the full-text one, else nothing."""
        item = self.lookup(text, vector)
        waited = time.monotonic()
        first = item.full if deep else item.fts
        if not first.done():
            # wait() never raises for the task, and a cancel of this pass still goes through.
            await asyncio.wait({first}, timeout=wait_ms / 1000)
        stats = dict(item.ms)
        stats["memory_wait_ms"] = int((time.monotonic() - waited) * 1000)
        for task in (first, item.fts):
            if task.done() and not task.cancelled() and task.exception() is None:
                return task.result(), stats
        return [], stats

    def close(self) -> None:
        for item in self.lookups.values():
            item.cancel()
        self.lookups.clear()
