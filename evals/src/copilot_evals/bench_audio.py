"""Generate the benchmark's "them" audio with text-to-speech.

    uv run copilot-bench-audio            # writes evals/bench/audio/mock_interview.wav

Each part of each utterance in evals/bench/mock_interview.toml is spoken by an OpenAI TTS voice
(synthetic, so no consent question), with the utterance's delivery `style` as the instruction.
Clips are cached by a hash of model, voice, style and text, so a rerun only pays for changed
lines. Leading and trailing silence is trimmed from each clip, then the clips are joined with
the script's gaps and pauses at 16 kHz mono, and a timeline JSON records where every utterance
(and each part) sits in the WAV, which the benchmark uses to score detections.

Cost: gpt-4o-mini-tts bills about $12 per million audio tokens; the whole script is about
2.5 minutes of speech, a few cents.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import wave
from pathlib import Path

import numpy as np

from copilot_evals.benchdata import AUDIO_DIR, SCRIPT, TIMELINE, WAV, Utterance, load_script

TTS_RATE = 24000  # OpenAI's pcm format: 24 kHz, 16-bit signed little-endian, mono
OUT_RATE = 16000
DEFAULT_MODEL = "gpt-4o-mini-tts"


def clip_key(model: str, voice: str, style: str, text: str) -> str:
    return hashlib.sha256("\0".join([model, voice, style, text]).encode()).hexdigest()[:24]


def trim(samples: np.ndarray, rate: int, threshold: float = 0.01, pad_ms: int = 30) -> np.ndarray:
    """Cut leading and trailing near-silence, keeping a short pad so word edges survive."""
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples[:0]
    pad = pad_ms * rate // 1000
    return samples[max(0, int(loud[0]) - pad) : min(samples.size, int(loud[-1]) + pad)]


async def synthesize(model: str, voice: str, style: str, text: str, cache: Path) -> np.ndarray:
    """One clip as float32 at OUT_RATE, from the cache or the TTS API."""
    import soxr  # pyright: ignore[reportMissingTypeStubs]

    path = cache / f"{clip_key(model, voice, style, text)}.pcm"
    if not path.exists():
        import openai

        client = openai.AsyncOpenAI()
        response = await client.audio.speech.create(
            model=model, voice=voice, input=text, instructions=style, response_format="pcm"
        )
        path.write_bytes(response.content)
    pcm = np.frombuffer(path.read_bytes(), "<i2").astype(np.float32) / 32768
    resampled: np.ndarray = soxr.resample(pcm, TTS_RATE, OUT_RATE)  # pyright: ignore[reportUnknownMemberType]
    return trim(resampled, OUT_RATE)


async def build(
    script: list[Utterance], model: str, cache: Path
) -> tuple[np.ndarray, list[dict[str, object]]]:
    jobs = [synthesize(model, u.voice, u.style, part, cache) for u in script for part in u.parts]
    clips = iter(await asyncio.gather(*jobs))
    chunks: list[np.ndarray] = []
    spans: list[dict[str, object]] = []
    pos = 0

    def silence(seconds: float) -> None:
        nonlocal pos
        n = round(seconds * OUT_RATE)
        chunks.append(np.zeros(n, np.float32))
        pos += n

    for u in script:
        silence(u.gap_s)
        start = pos
        parts: list[list[float]] = []
        for i, _ in enumerate(u.parts):
            if i:
                silence(u.pause_s)
            clip = next(clips)
            parts.append([round(pos / OUT_RATE, 3), round((pos + clip.size) / OUT_RATE, 3)])
            chunks.append(clip)
            pos += clip.size
        spans.append(
            {
                "id": u.id,
                "start_s": round(start / OUT_RATE, 3),
                "end_s": round(pos / OUT_RATE, 3),
                "parts": parts,
            }
        )
    silence(6.0)  # room for the last answers
    return np.concatenate(chunks), spans


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm)


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the benchmark audio with TTS.")
    p.add_argument("--script", type=Path, default=SCRIPT)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--out", type=Path, default=WAV)
    p.add_argument("--timeline", type=Path, default=TIMELINE)
    args = p.parse_args(argv)

    from copilot_core.dotenv import load_dotenv

    load_dotenv()
    script = load_script(args.script)
    (AUDIO_DIR / "clips").mkdir(parents=True, exist_ok=True)
    samples, spans = await build(script, args.model, AUDIO_DIR / "clips")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(args.out, samples)
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    args.timeline.write_text(
        json.dumps(
            {"wav": args.out.name, "sha256": digest, "model": args.model, "spans": spans},
            indent=2,
        ),
        "utf-8",
    )
    speech = sum(float(s["end_s"]) - float(s["start_s"]) for s in spans)  # pyright: ignore[reportArgumentType]
    print(
        f"wrote {args.out} ({samples.size / OUT_RATE:.1f} s, {speech:.1f} s of speech, "
        f"{len(spans)} utterances) and {args.timeline}"
    )
    return 0


def run() -> None:
    sys.exit(asyncio.run(main()))
