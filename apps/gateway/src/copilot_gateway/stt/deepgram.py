"""Deepgram streaming STT over its WebSocket API (live transcription, `/v1/listen`)."""

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from copilot_gateway.stt.base import STTResult

SAMPLE_RATE = 16000


class DeepgramSTT:
    name = "deepgram"

    def __init__(
        self, api_key: str, *, url: str, model: str, keyterms: list[str] | None = None
    ) -> None:
        self.api_key = api_key
        params: list[tuple[str, str]] = [
            ("model", model),
            ("encoding", "linear16"),
            ("sample_rate", str(SAMPLE_RATE)),
            ("channels", "1"),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("smart_format", "true"),
        ]
        params += [("keyterm", term) for term in keyterms or []]
        self.url = f"{url}?{urlencode(params)}"
        self.ws: ClientConnection | None = None

    async def connect(self) -> None:
        self.ws = await connect(
            self.url,
            additional_headers={"Authorization": f"Token {self.api_key}"},
            open_timeout=5,
            max_size=2**20,
        )

    def _conn(self) -> ClientConnection:
        if self.ws is None:
            raise ConnectionError("deepgram stream is not connected")
        return self.ws

    async def send_audio(self, pcm: bytes) -> None:
        await self._conn().send(pcm)

    async def finalize(self) -> None:
        await self._conn().send(json.dumps({"type": "Finalize"}))

    async def keepalive(self) -> None:
        await self._conn().send(json.dumps({"type": "KeepAlive"}))

    async def results(self) -> AsyncIterator[STTResult]:
        async for raw in self._conn():
            if isinstance(raw, bytes):
                continue
            result = parse_message(json.loads(raw))
            if result is not None:
                yield result

    async def close(self) -> None:
        if self.ws is None:
            return
        with contextlib.suppress(Exception):
            await self.ws.send(json.dumps({"type": "CloseStream"}))
        await self.ws.close()


def parse_message(msg: dict[str, Any]) -> STTResult | None:
    """A Deepgram `Results` message as an STTResult; other message types give None."""
    if msg.get("type") != "Results":
        return None
    channel: dict[str, Any] = msg.get("channel") or {}
    alternatives: list[dict[str, Any]] = channel.get("alternatives") or [{}]
    start = float(msg.get("start", 0.0))
    duration = float(msg.get("duration", 0.0))
    return STTResult(
        text=str(alternatives[0].get("transcript", "")),
        is_final=bool(msg.get("is_final")),
        start_ms=round(start * 1000),
        end_ms=round((start + duration) * 1000),
        flushed=bool(msg.get("from_finalize")),
    )
