"""Client-side voice activity detection with Silero VAD, as the desktop client does it.

Causal, like Silero's own VADIterator: speech starts when a 32 ms window scores at least
`threshold`; it ends once the score has stayed under `threshold - 0.15` for `min_silence_ms`.
Each event records both when the client can decide it (`decided_at`) and where the speech
really starts or ends in the audio (`edge`); the gap at the end is the VAD hangover, which
counts against first-word latency.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

SAMPLE_RATE = 16000
WINDOW = 512  # samples, 32 ms; fixed by Silero v5 and later
WINDOW_BYTES = WINDOW * 2


@dataclass(frozen=True)
class Segment:
    start_edge: int  # sample where speech starts
    start_decided: int  # sample at which the client knows (end of the triggering window)
    end_edge: int  # sample just after the last voiced window
    end_decided: int  # sample at which min_silence_ms has passed

    @property
    def hangover_ms(self) -> int:
        return (self.end_decided - self.end_edge) * 1000 // SAMPLE_RATE


def silero_probabilities(pcm: bytes) -> list[float]:
    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    return [
        vad(pcm[i : i + WINDOW_BYTES]) for i in range(0, len(pcm) - WINDOW_BYTES + 1, WINDOW_BYTES)
    ]


def segment(
    probabilities: Iterable[float],
    *,
    threshold: float = 0.5,
    min_silence_ms: int = 200,
    min_speech_ms: int = 120,
    total_samples: int | None = None,
) -> list[Segment]:
    neg = max(0.01, threshold - 0.15)
    min_silence = min_silence_ms * SAMPLE_RATE // 1000
    min_speech = min_speech_ms * SAMPLE_RATE // 1000
    out: list[Segment] = []
    start: tuple[int, int] | None = None
    silence_from: int | None = None
    last = 0
    for i, p in enumerate(probabilities):
        w_start, w_end = i * WINDOW, (i + 1) * WINDOW
        last = w_end
        if start is None:
            if p >= threshold:
                start = (w_start, w_end)
                silence_from = None
            continue
        if p >= threshold:
            silence_from = None
        elif p < neg:
            if silence_from is None:
                silence_from = w_start
            if w_end - silence_from >= min_silence:
                if silence_from - start[0] >= min_speech:
                    # A client that waits min_speech_ms before sending can drop blips.
                    decided = max(start[1], start[0] + min_speech)
                    out.append(Segment(start[0], decided, silence_from, w_end))
                start, silence_from = None, None
    if start is not None:
        end = total_samples if total_samples is not None else last
        edge = silence_from if silence_from is not None else end
        out.append(Segment(start[0], max(start[1], start[0] + min_speech), edge, end))
    return out


type ProbabilityFn = Callable[[bytes], list[float]]
