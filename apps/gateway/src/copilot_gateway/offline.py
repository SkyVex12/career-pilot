"""Run the real gateway with scripted STT and Claude (copilot_gateway.testing), so a client can
be tested end to end without provider keys or the network.

    uv run python -m copilot_gateway.offline --port 8081 --token dev-token-0123456789
    uv run python -m copilot_gateway.offline --valkey redis://localhost:6379/15 ...

The first form keeps session state in an in-process fake Valkey, so a session survives a
dropped socket but not a restart of this process. With --sqs the session log jobs
(session.flush, session.ended) go to the compose stack's queue, so a worker turns the call
into rows and post-call outputs; use it with --valkey pointing at the workers' Valkey.
To resume across a restart, point every process at the same real Valkey (the compose stack's)
with --valkey. fakeredis's TCP server cannot stand in: on Windows it deadlocks on pipelined
commands.

The STT is `TextSTT`: it reads the audio bytes as UTF-8 words, so a client "speaks" by sending
text padded to pcm16 frames (`testing.text_frame_audio`), on either channel: the user's own
mic stream is a second TextSTT. Real microphone audio produces no transcript; typed questions
still get scripted answers.

The process prints one line, `listening <port>`, once it accepts connections. The desktop
client's integration test (apps/desktop/core/tests/gateway.rs) starts these processes.
"""

import argparse
import asyncio
import contextlib
import sys
import time
from typing import Any
from uuid import UUID

from copilot_core.jobs import JobQueue, MemoryJobQueue, SqsJobQueue
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore
from copilot_gateway.testing import GatewayServer, ScriptedProvider, TextSTT, TextSTTControl

DEV_USER = UUID("0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b")

# Shorter timers than production, as in the gateway's own tests, so a test call is quick.
FAST_TIMERS: dict[str, Any] = {
    "speculative_start_ms": 100,
    "silence_timer_ms": 250,
    "final_wait_max_ms": 500,
    "lease_renew_s": 0.5,
    "lease_ttl_s": 3.0,
}


def parse_token(spec: str) -> tuple[str, float | None]:
    """`TOKEN` never expires; `TOKEN:SECONDS` expires that many seconds after startup."""
    token, _, seconds = spec.partition(":")
    return token, (time.time() + float(seconds)) if seconds else None


async def serve_gateway(args: argparse.Namespace) -> None:
    overrides: dict[str, Any] = {
        "env": "test",
        "llm_providers": ["anthropic"],
        "embedder": "none",
        "valkey_url": args.valkey if args.valkey != "fake" else "redis://unused",
        **(FAST_TIMERS if args.fast_timers else {}),
        **({"min_client_version": args.min_client_version} if args.min_client_version else {}),
    }
    settings = Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
    if args.valkey == "fake":
        from fakeredis import FakeAsyncValkey

        client: Any = FakeAsyncValkey()
    else:
        from copilot_gateway.store import make_client

        client = make_client(settings)
    principals = {
        token: Principal(DEV_USER, "Dev User", expires)
        for token, expires in map(parse_token, args.token)
    }
    control = TextSTTControl()
    me_control = TextSTTControl()
    queue: JobQueue = MemoryJobQueue()
    if args.sqs:
        queue = SqsJobQueue(
            settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint or None
        )
    services = Services(
        settings=settings,
        store=GatewayStore(client, settings),
        jobs=queue,
        router=build_router(settings, [ScriptedProvider("anthropic")]),
        verifier=StaticVerifier(principals),
        stt_providers=[("text", lambda: TextSTT(control, "text"))],
        stt_me_providers=[("text", lambda: TextSTT(me_control, "text"))],
    )
    server = GatewayServer(services)
    server.server.config.port = args.port
    async with server:
        print(f"listening {server.port}", flush=True)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gateway with scripted STT and Claude.")
    parser.add_argument("--port", type=int, default=8081, help="0 picks a free port")
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        help="accepted bearer token for the dev user; TOKEN:SECONDS expires (repeatable)",
    )
    parser.add_argument("--valkey", default="fake", help="Valkey URL, or `fake` (in process)")
    parser.add_argument(
        "--fast-timers", action="store_true", help="shorter turn and lease timers, as in tests"
    )
    parser.add_argument(
        "--sqs", action="store_true", help="send jobs to the local SQS queue (default: dropped)"
    )
    parser.add_argument(
        "--min-client-version", default="", help="the oldest desktop app served (e.g. 0.2.0)"
    )
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("give at least one --token")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_gateway(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
