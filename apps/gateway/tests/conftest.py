"""Fixtures for gateway tests: an in-memory Valkey (fakeredis), scripted STT and Claude, and a
real uvicorn server per test. Timers are shortened so the tests run in seconds."""

import time
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import pytest
from fakeredis import FakeAsyncValkey, FakeServer

from copilot_core.jobs import MemoryJobQueue
from copilot_core.llm import LLMProvider
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore
from copilot_gateway.testing import ScriptedProvider, TextSTT, TextSTTControl

USER_A = UUID("0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b")
USER_B = UUID("0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6c")


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "speculative_start_ms": 100,
        "silence_timer_ms": 250,
        "final_wait_max_ms": 500,
        "first_token_timeout_ms": 1000,
        "stt_stall_s": 0.4,
        "stt_keepalive_s": 1.0,
        "lease_renew_s": 0.2,
        "lease_ttl_s": 2.0,
        "flush_interval_s": 60.0,
        "resume_window_s": 30.0,
        "first_message_timeout_s": 0.5,
        "force_min_interval_s": 1.0,
        "llm_providers": ["anthropic"],
        "embedder": "none",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


class Env:
    """One test's world: a shared fake Valkey, a job queue, scripted providers and STT."""

    def __init__(self) -> None:
        self.user_a = USER_A
        self.user_b = USER_B
        self.valkey_server = FakeServer()
        self.jobs = MemoryJobQueue()
        self.stt = TextSTTControl()
        self.stt_me = TextSTTControl()  # the user's mic stream
        self.providers: list[LLMProvider] = [ScriptedProvider("anthropic")]
        self.verifier = StaticVerifier(
            {
                "token-a": Principal(USER_A, "Alex", None),
                "token-a2": Principal(USER_A, "Alex", None),
                "token-b": Principal(USER_B, "Blair", None),
            }
        )

    def settings(self, **overrides: Any) -> Settings:
        return make_settings(**overrides)

    def client(self) -> FakeAsyncValkey:
        return FakeAsyncValkey(server=self.valkey_server)

    def services(
        self,
        settings: Settings | None = None,
        stt_names: tuple[str, ...] = ("text",),
        **extra: Any,
    ) -> Services:
        settings = settings or make_settings()

        def factory(
            name: str, control: TextSTTControl | None = None
        ) -> Callable[[Sequence[str]], TextSTT]:
            def make(keyterms: Sequence[str]) -> TextSTT:
                (control or self.stt).keyterms.append(list(keyterms))
                return TextSTT(control or self.stt, name)

            return make

        return Services(
            settings=settings,
            store=GatewayStore(self.client(), settings),
            jobs=self.jobs,
            router=build_router(settings, self.providers),
            verifier=self.verifier,
            stt_providers=[(n, factory(n)) for n in stt_names],
            stt_me_providers=[(n, factory(n, self.stt_me)) for n in stt_names],
            **extra,
        )

    def wait_until(self, cond: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                raise AssertionError("condition not met")
            time.sleep(0.01)


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return make_settings
