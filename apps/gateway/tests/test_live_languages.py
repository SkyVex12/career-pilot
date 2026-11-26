"""Live-language boundaries use one paid stream per channel and preserve the call."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from copilot_core import companions
from copilot_gateway.companionhost import CompanionFold
from copilot_gateway.seatview import guest_message
from copilot_gateway.stt.deepgram import DeepgramSTT
from copilot_gateway.stt.resilient import TimedResult
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    TextSTT,
    TextSTTControl,
    text_frame_audio,
)


class SpeechProbe:
    def __init__(self):
        self.streams = []
        self.active = Counter()
        self.maximum = Counter()
        self.fail = set()
        self.block_language: str | None = None
        self.connecting = asyncio.Event()
        self.release = asyncio.Event()

    def factory(self, channel):
        def make(terms):
            stream = LocalDeepgram(self, channel)
            self.streams.append(stream)
            return stream

        return make


class LocalDeepgram(DeepgramSTT):
    """Exercise the real language configuration with a deterministic local transport."""

    def __init__(self, probe, channel):
        super().__init__("fake", url="wss://example.invalid/listen", model="nova-3")
        self.probe = probe
        self.channel = channel
        self.text = TextSTT(TextSTTControl(), "deepgram")
        self.open = False
        self.closed = False
        self.connections = 0

    @property
    def language(self):
        return parse_qs(urlsplit(self.url).query)["language"][0]

    async def connect(self):
        self.connections += 1
        if self.language == self.probe.block_language:
            self.probe.connecting.set()
            await self.probe.release.wait()
        if (self.channel, self.language) in self.probe.fail:
            raise ConnectionError("credential detail must never reach the client")
        self.open = True
        self.probe.active[self.channel] += 1
        self.probe.maximum[self.channel] = max(
            self.probe.maximum[self.channel], self.probe.active[self.channel]
        )
        await self.text.connect()

    async def close(self):
        self.closed = True
        if self.open:
            self.open = False
            self.probe.active[self.channel] -= 1
        await self.text.close()

    async def send_audio(self, pcm):
        await self.text.send_audio(pcm)

    async def finalize(self):
        await self.text.finalize()

    async def keepalive(self):
        await self.text.keepalive()

    async def results(self):
        async for result in self.text.results():
            yield result


def service(env, **settings):
    probe = SpeechProbe()
    svc = env.services(env.settings(stt_providers=["deepgram"], **settings))
    svc.stt_providers = [("deepgram", probe.factory("them"))]
    svc.stt_me_providers = [("deepgram", probe.factory("me"))]
    return svc, probe


async def until(check: Callable[[], bool]):
    async with asyncio.timeout(3):
        while not check():  # noqa: ASYNC110 - server state has no test notification hook
            await asyncio.sleep(0.005)


async def change(client, language, request_id=None):
    rid = str(request_id or uuid4())
    start = len(client.messages)
    await client.send(
        {"type": "session.set_language", "spoken_language": language, "request_id": rid}
    )
    return await client.wait(
        lambda m: m["type"] == "session.language" and m.get("request_id") == rid,
        start=start,
    )


async def test_live_switch_preserves_history_and_resumes_midutterance_on_both_channels(env):
    svc, probe = service(env)
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        ready = await client.start()
        assert ready["spoken_language"] == "en"
        assert ready["language_switching"]
        session = gateway.manager.sessions[UUID(client.session_id)]
        await until(lambda: probe.active == {"them": 1, "me": 1})
        await client.say("The earlier completed sentence.", capture_ms=100)
        await client.wait_type("transcript.final", text="The earlier completed sentence.")
        old = session.stt
        old_mic = session.stt_me
        original_version = session.cache_version()
        old_lines = list(session.conv.lines)
        # An old-language partial is deliberately in progress at the switch boundary.
        await client.frame(0, 1, text_frame_audio("unfinished English"), capture_ms=500)
        await client.wait_type("transcript.partial", text="unfinished English")
        event = await change(client, "es")
        assert event["spoken_language"] == "es"
        assert "error" not in event
        assert session.sid == UUID(client.session_id)
        assert session.conv.lines == old_lines
        assert session.spoken_language == session.info.spoken_language == "es"
        assert session.prompt_context().spoken_language == session.detector.spoken_language == "es"
        assert session.turns.spoken_language == "es"
        assert not session.turns.utterance()
        assert session.cache_version() != original_version
        assert not session.detector.cache
        await until(lambda: probe.active == {"them": 1, "me": 1})
        assert len(probe.streams) == 4
        assert all(s.connections == 1 for s in probe.streams)
        assert all(s.closed for s in probe.streams[:2])
        assert probe.maximum == {"them": 1, "me": 1}
        # Late callbacks cannot append old results or overwrite the new status.
        seq = session.seq
        for previous in (old, old_mic):
            await previous.on_result(TimedResult("stale", True, True, 0, 100, "deepgram"))
            previous.on_status("reconnecting", "stale stream")
        assert session.seq == seq
        # The capture client need not issue a fresh VAD start after a language boundary.
        await client.frame(0, 0, text_frame_audio("Una nueva frase."), capture_ms=1000)
        await client.frame(0, 2, capture_ms=1100)
        await client.wait_type("transcript.final", text="Una nueva frase.", channel="them")
        await client.frame(1, 0, text_frame_audio("Mi propia frase."), capture_ms=5000)
        await client.frame(1, 2, capture_ms=5100)
        await client.wait_type("transcript.final", text="Mi propia frase.", channel="me")
        assert all(
            "unfinished" not in " ".join(s.text.control.sent_text) for s in probe.streams[2:]
        )
        assert await session.writer.drain(within_s=1)
        state = await svc.store.load_state(session.sid)
        assert state["spoken_language"] == "es"
        assert state["language_switching"] == "1"
        cursor = client.last_seq
        await client.drop()
        await client.resume()
        event = await client.wait(lambda m: m["type"] == "session.language" and m["seq"] > cursor)
        assert event["spoken_language"] == "es"
        assert not event.get("request_id")
        assert len(probe.streams) == 4  # resume reattaches the same capture owner
        await client.close()
    assert not any(probe.active.values())


async def test_failed_second_channel_closes_prepared_stream_and_restores_previous_language(env):
    svc, probe = service(env)
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start()
        session = gateway.manager.sessions[UUID(client.session_id)]
        await until(lambda: probe.active == {"them": 1, "me": 1})
        probe.fail.add(("me", "es"))
        request_id = uuid4()
        event = await change(client, "es", request_id)
        assert event["spoken_language"] == "en"
        assert "Could not connect" in event["error"]
        assert "credential" not in event["error"]
        assert session.prompt_context().spoken_language == session.turns.spoken_language == "en"
        await until(lambda: probe.active == {"them": 1, "me": 1})
        assert all(s.closed for s in probe.streams if s.language == "es")
        assert probe.maximum == {"them": 1, "me": 1}
        await client.say("Still connected in the previous language.", capture_ms=1000)
        await client.wait_type("transcript.final", text="Still connected in the previous language.")
        # A failed request may be retried after recovery with its original id.
        probe.fail.clear()
        event = await change(client, "es", request_id)
        assert "error" not in event
        assert event["spoken_language"] == "es"
        await client.close()
    assert not any(probe.active.values())


async def test_invalid_unavailable_and_idempotent_requests_do_not_replace_streams(env):
    svc, probe = service(env, verified_call_languages=("en", "es"))
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start()
        await until(lambda: len(probe.streams) == 2)
        for code in ("xx", "ja"):
            event = await change(client, code)
            assert event["spoken_language"] == "en"
            assert event["error"]
            assert len(probe.streams) == 2
        request_id = uuid4()
        assert "error" not in await change(client, "es", request_id)
        assert "error" not in await change(client, "es", request_id)
        assert len(probe.streams) == 4
        event = await change(client, "en", request_id)
        assert event["spoken_language"] == "es"
        assert "already been used" in event["error"]
        assert len(probe.streams) == 4
        await client.close()


async def test_disconnect_during_provider_preparation_finishes_transaction_and_resumes(env):
    svc, probe = service(env)
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start()
        session = gateway.manager.sessions[UUID(client.session_id)]
        await until(lambda: probe.active == {"them": 1, "me": 1})
        probe.block_language = "ja"
        rid = str(uuid4())
        await client.send(
            {"type": "session.set_language", "spoken_language": "ja", "request_id": rid}
        )
        await asyncio.wait_for(probe.connecting.wait(), 1)
        await client.drop()
        probe.release.set()
        await until(lambda: session.spoken_language == "ja")
        await client.resume()
        event = await client.wait_type("session.language", request_id=rid)
        assert event["spoken_language"] == "ja"
        assert "error" not in event
        assert probe.maximum == {"them": 1, "me": 1}
        await client.close()


def test_companion_and_guest_snapshot_keep_only_current_language_not_transient_errors():
    fold = CompanionFold()
    for seq, language in enumerate(("en", "es", "ja"), 1):
        message = {
            "type": "session.language",
            "seq": seq,
            "spoken_language": language,
            "request_id": str(uuid4()),
            "error": "temporary detail",
        }
        fold.add_message(message)
    expected = {"type": "session.language", "seq": 3, "spoken_language": "ja"}
    assert fold.messages() == [expected]
    private = {**expected, "request_id": str(uuid4()), "error": "private detail"}
    rendered = guest_message(json.dumps(private))
    assert rendered is not None
    assert json.loads(rendered) == expected
    restored = CompanionFold.load(fold.dump())
    assert restored is not None
    assert restored.messages() == [expected]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "set_language"},
        {"kind": "answer", "spoken_language": "es"},
        {"kind": "set_language", "spoken_language": "es", "text": "extra"},
    ],
)
def test_companion_language_command_requires_only_the_language(body):
    with pytest.raises(ValidationError):
        companions.Command.model_validate(
            {"type": "companion.command", "command_id": uuid4(), **body}
        )


async def test_switch_cancels_pending_answers_and_keeps_completed_cards_and_cache_separate(env):
    from copilot_gateway.testing import Reply, ScriptedProvider, default_reply

    slow = False

    def reply(job, request):
        system = " ".join(block.text for block in request.system)
        if slow and job == "fast" and "Spanish" not in system:
            return Reply(["old-language answer"], first_delay_s=5)
        return default_reply(job, request)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    svc, _ = service(env, force_min_interval_s=0)
    async with GatewayServer(svc) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start()
        session = gateway.manager.sessions[UUID(client.session_id)]
        await client.send({"type": "question.force", "text": "Tell me about an earlier project"})
        completed = await client.wait_type("answer.done", pass_="fast")
        old_version = session.cache_version()
        slow = True
        await client.send({"type": "question.force", "text": "Explain the interrupted project"})
        question = await client.wait_type(
            "question.detected", text="Explain the interrupted project"
        )
        await until(
            lambda: any(
                job == "fast" and "interrupted project" in str(req.content)
                for job, req in provider.requests
            )
        )
        event = await change(client, "es")
        assert "error" not in event
        await client.wait_type(
            "answer.error", question_id=question["question_id"], code="cancelled"
        )
        assert UUID(completed["question_id"]) in session.answers.questions
        assert all(
            task.done() for q in session.answers.questions.values() for task in q.tasks.values()
        )
        assert provider.cancelled >= 1
        # Even a background write carrying the old snapshot cannot masquerade as Spanish.
        await session.answers._cache_put(
            "identical-question", "fast", "old cached answer", [], old_version
        )
        assert await session.answers._cached("identical-question", "fast") is None
        assert (await session.answers._cached("identical-question", "fast", old_version))[
            "text"
        ] == "old cached answer"
        await client.send({"type": "question.force", "text": "Explica el proyecto nuevo"})
        following = await client.wait_type("question.detected", text="Explica el proyecto nuevo")
        await client.wait_type("answer.done", question_id=following["question_id"], pass_="fast")
        latest = next(req for job, req in reversed(provider.requests) if job == "fast")
        assert "Spanish" in " ".join(block.text for block in latest.system)
        assert not any(
            m.get("text") == "old-language answer" for m in client.of_type("answer.delta")
        )
        await client.close()


async def test_companion_command_switches_host_and_replays_current_language(env, monkeypatch):
    import time

    from copilot_core.auth import AuthError
    from copilot_gateway.companionrelay import CompanionRelay

    async def checked(self, principal, sid, aid):
        grant = await companions.read(self.svc.store.client, sid, aid)
        live = await self.svc.store.load_state(sid)
        if not companions.allowed(grant, live, str(principal.user_id)):
            raise AuthError("denied")
        return grant

    monkeypatch.setattr(CompanionRelay, "authorize", checked)
    svc, probe = service(env)
    async with GatewayServer(svc) as gateway:
        host = ProtocolClient(gateway.url)
        await host.start()
        sid = host.session_id
        grant = companions.Attachment(
            str(uuid4()), str(env.user_a), str(env.user_a), sid, "Phone", time.time() + 60
        )
        await companions.publish(env.client(), grant)
        phone = ProtocolClient(gateway.url.replace("/v1/session", "/v1/companion"))
        await phone.connect()
        join = {"type": "companion.join", "session_id": sid, "attachment_id": grant.attachment_id}
        await phone.send(join)
        ready = await phone.wait_type("companion.ready")
        assert ready["spoken_language"] == "en"
        assert ready["language_switching"]
        request_id = str(uuid4())
        command = {
            "type": "companion.command",
            "command_id": request_id,
            "kind": "set_language",
            "spoken_language": "ja",
        }
        await phone.send(command)
        await phone.wait_type("companion.command.result", result="executed")
        event = await phone.wait_type("session.language", request_id=request_id)
        assert event["spoken_language"] == "ja"
        assert (await host.wait_type("session.language", request_id=request_id))[
            "spoken_language"
        ] == "ja"
        assert probe.maximum == {"them": 1, "me": 1}
        await phone.close()
        resumed = ProtocolClient(phone.url)
        await resumed.connect()
        await resumed.send(join)
        assert (await resumed.wait_type("companion.ready"))["spoken_language"] == "ja"
        snapshot = await resumed.wait_type("companion.snapshot")
        current = [m for m in snapshot["messages"] if m["type"] == "session.language"]
        assert len(current) == 1
        assert current[0]["spoken_language"] == "ja"
        assert "request_id" not in current[0]
        await resumed.send(command)
        await resumed.wait_type("companion.command.result", result="executed")
        assert len(probe.streams) == 4
        await resumed.close()
        await host.close()


async def test_cancel_during_prepared_stream_adoption_closes_the_owned_socket():
    from unittest.mock import AsyncMock

    from copilot_gateway.stt.resilient import ResilientSTT

    replaying = asyncio.Event()
    closed = asyncio.Event()

    class BlockedReplay(TextSTT):
        async def send_audio(self, pcm):
            replaying.set()
            await asyncio.Event().wait()

        async def close(self):
            closed.set()
            await super().close()

    control = TextSTTControl()
    transport = BlockedReplay(control)
    stream = ResilientSTT([("text", lambda _: transport)], AsyncMock(), lambda *_: None)
    await stream.prepare()
    await stream.send(b"\0" * 3200, 1234)
    running = asyncio.create_task(stream.run())
    await asyncio.wait_for(replaying.wait(), 1)
    running.cancel()
    await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 1)
    assert closed.is_set()
    assert stream.stream is None
    assert control.connects == 1


async def test_language_boundary_terminates_answer_cancelled_before_first_coroutine_step(
    settings_factory,
):
    from types import SimpleNamespace
    from typing import Any, cast

    from copilot_gateway.answer import AnswerEngine, Question
    from copilot_protocol import models

    events = []
    host = SimpleNamespace(
        settings=settings_factory(),
        emit=lambda model, **fields: events.append((model, fields)),
        log_entry=lambda entry: None,
    )
    engine = AnswerEngine(cast(Any, host))
    question = Question(
        qid=uuid4(),
        text="What is the next step?",
        qtype="other",
        confidence=1,
        addressed=True,
        speculative=False,
        forced=True,
        t0=0,
        detector_ms=None,
        match=None,
    )
    engine.add(question)
    # There is deliberately no yield between scheduling the pass and changing language.
    await engine.change_language()
    assert question.tasks["fast"].cancelled()
    assert events == [
        (
            models.AnswerError,
            {
                "question_id": question.qid,
                "pass": "fast",
                "code": "cancelled",
            },
        )
    ]
