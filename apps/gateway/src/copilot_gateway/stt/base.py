"""The STTStream interface every provider implements."""

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class STTResult:
    text: str
    is_final: bool
    # Milliseconds on this connection's audio timeline (audio sent since connect).
    start_ms: int
    end_ms: int
    # True on the final that answers finalize(): the provider has flushed everything sent.
    flushed: bool = False


class STTStream(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def send_audio(self, pcm: bytes) -> None:
        """16 kHz mono pcm16."""
        ...

    async def finalize(self) -> None:
        """Ask for finals on everything sent so far (sent on a VAD speech end)."""
        ...

    async def keepalive(self) -> None:
        """Keep the connection open while no audio flows."""
        ...

    def results(self) -> AsyncIterator[STTResult]:
        """Results until the connection ends. Ending or raising both count as a drop."""
        ...

    async def close(self) -> None: ...


# Makes a stream; the argument is the words to expect (keyterms), possibly none.
type STTFactory = Callable[[Sequence[str]], STTStream]
