"""Restartable Ask/practice execution, with PostgreSQL as the accepted-work record.

The API process hosts a small, separately bounded executor on Stage 0. It can be run
independently with the same service dependencies later. Requests only commit operations;
no HTTP response owns a model task and no model stream holds a database connection.
"""

import asyncio
import contextlib
import logging
import signal
from typing import Any
from uuid import UUID, uuid4

from copilot_api.deps import Services
from copilot_core import careeros, operations, profiles
from copilot_core.auth import Principal
from copilot_core.db import actor_account

log = logging.getLogger(__name__)


class InteractiveRunner:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.owner = uuid4()
        self.running: set[asyncio.Task[None]] = set()
        self.stop = asyncio.Event()

    async def claim(self) -> tuple[dict[str, Any], operations.Lease] | None:
        async with self.svc.pool.acquire() as conn, conn.transaction():
            return await operations.claim_next(
                conn, self.owner, self.svc.settings.interactive_lease_s, lane="interactive"
            )

    async def run(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    while len(self.running) < self.svc.settings.interactive_concurrency:
                        claimed = await self.claim()
                        if claimed is None:
                            break
                        task = asyncio.create_task(self.process(*claimed))
                        self.running.add(task)
                        task.add_done_callback(self.running.discard)
                except Exception:
                    log.exception("interactive operation claim unavailable")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=1.0)
        finally:
            for task in self.running:
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)

    async def drain_ready(self) -> int:
        """Deterministic single-executor entry point, also useful for integration tests."""
        count = 0
        while claimed := await self.claim():
            await self.process(*claimed)
            count += 1
        return count

    async def _heartbeat(self, lease: operations.Lease, task: asyncio.Task[Any]) -> None:
        try:
            while True:
                await asyncio.sleep(self.svc.settings.interactive_lease_s / 3)
                async with self.svc.pool.acquire() as conn, conn.transaction():
                    alive = await operations.renew(
                        conn, lease, self.svc.settings.interactive_lease_s
                    )
                if not alive:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failure to prove ownership stops provider work; the expired lease is recoverable.
            log.warning("interactive operation renewal unavailable", exc_info=True)
            task.cancel()

    async def process(self, message: dict[str, Any], lease: operations.Lease) -> None:
        from copilot_api.routes import ask, practice

        actor_token = actor_account.set(None)
        lease_token = operations.current_lease.set(lease)
        task = asyncio.current_task()
        assert task is not None
        heartbeat = asyncio.create_task(self._heartbeat(lease, task))
        completed = False
        try:
            payload = message["payload"]
            account_id = UUID(payload["account_id"])
            workspace_id = UUID(payload["workspace_id"])
            actor_account.set(account_id)
            account = Principal(account_id, "", None)
            async with self.svc.tx(account) as conn:
                name = await conn.fetchval(
                    "select display_name from app.accounts where id=$1", account_id
                )
                if name is None:
                    raise profiles.ProfileError("account no longer exists")
                if self.svc.settings.require_career_os and not await careeros.is_linked(conn):
                    raise profiles.ProfileError("account link is no longer active")
            account = Principal(account_id, name, None)
            user = await profiles.resolve(self.svc.pool, account, str(workspace_id))
            if user.user_id != workspace_id:
                raise profiles.ProfileError("workspace selection changed")
            if message["type"] == "ask.generate":
                await ask.generate(
                    self.svc,
                    user,
                    account,
                    UUID(payload["conversation_id"]),
                    UUID(payload["message_id"]),
                    int(payload["attempt"]),
                )
            elif message["type"] == "practice.generate":
                await practice.generate(
                    self.svc,
                    user,
                    account,
                    UUID(payload["session_id"]),
                    UUID(payload["operation_id"]),
                )
            else:
                raise ValueError("unsupported interactive operation")
            completed = True
        except asyncio.CancelledError:
            # Domain cancellation/attempt guards prevent a restarted job from publishing
            # cancelled output. A process shutdown leaves pending work recoverable.
            pass
        except operations.LeaseLost:
            pass
        except profiles.ProfileError:
            # Access revoked after acceptance: no model call, and no retry can restore access.
            completed = True
            log.info("interactive operation no longer authorized")
        except Exception:
            log.exception("interactive operation interrupted")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            operations.current_lease.reset(lease_token)
            actor_account.reset(actor_token)
            with contextlib.suppress(Exception):
                async with self.svc.pool.acquire() as conn, conn.transaction():
                    if completed:
                        await operations.finish(conn, lease)
                    else:
                        await operations.fail(conn, lease, "interactive execution interrupted", 5)


async def main() -> None:
    from copilot_api.config import ApiSettings
    from copilot_api.main import build_services

    svc = await build_services(ApiSettings())
    runner = InteractiveRunner(svc)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runner.stop.set)
    try:
        await runner.run()
    finally:
        await svc.pool.close()
        await svc.valkey.aclose()


def run() -> None:
    from copilot_core import logs
    from copilot_core.dotenv import load_dotenv

    load_dotenv()
    logs.configure("interactive")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
