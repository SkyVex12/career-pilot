"""Pre-recorded speech to text for the user's own audio (ARCHITECTURE.md, "Session logging").

Deepgram's `/v1/listen` with the whole recording as the body; `utterances=true` returns
speaker turns with start and end times, which become the "me" transcript segments. The
recording starts at the session's capture clock zero, so its times line up with the live
"them" segments.
"""

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx2


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str


class PrerecordedSTT(Protocol):
    async def transcribe(self, audio: bytes, content_type: str) -> list[Segment]: ...


class DeepgramPrerecorded:
    def __init__(
        self,
        api_key: str,
        url: str = "https://api.deepgram.com/v1/listen",
        model: str = "nova-3",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.url = url
        self.model = model
        self.transport = transport

    async def transcribe(self, audio: bytes, content_type: str) -> list[Segment]:
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
        }
        async with httpx2.AsyncClient(timeout=300.0, transport=self.transport) as client:
            response = await client.post(
                self.url,
                params=params,
                content=audio,
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": content_type,
                },
            )
            response.raise_for_status()
            return parse_utterances(response.json())


def parse_utterances(data: dict[str, Any]) -> list[Segment]:
    results = cast(dict[str, Any], data.get("results") or {})
    out: list[Segment] = []
    for u in cast(list[dict[str, Any]], results.get("utterances") or []):
        text = str(u.get("transcript", "")).strip()
        if text:
            out.append(
                Segment(
                    start_ms=round(float(u["start"]) * 1000),
                    end_ms=round(float(u["end"]) * 1000),
                    text=text,
                )
            )
    return out
