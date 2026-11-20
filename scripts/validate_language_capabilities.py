"""Opt-in, bounded synthetic provider check; no private records or server writes.

Run with --live --output .implementation/language-provider-check.json.
This sends at most four speech requests, one embedding batch and two answer requests.
Synthetic speech is a wire smoke test, not accent or real-device certification.
"""

import argparse
import asyncio
import inspect
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from copilot_api.config import ApiSettings
from copilot_api.routes import ask as routes
from copilot_api.routes import practice
from copilot_core import ask, embed


async def run(corpus: list[dict[str, str]]) -> dict[str, Any]:
    settings = ApiSettings()
    svc: Any = SimpleNamespace(settings=settings)
    receipt: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "synthetic_only": True,
        "device_certification": False,
        "speech": [],
        "answers": [],
    }
    for language in ("es", "ja"):
        case = next(c for c in corpus if c["language"] == language)
        started = time.monotonic()
        audio = await practice.voice_request(svc, "speech", b"", "", language, case["question"])
        transcript = (
            await practice.voice_request(
                svc, "transcription", audio, "audio/mpeg", language, keyterms=("PostgreSQL", "Acme")
            )
        ).decode()
        receipt["speech"].append(
            {
                "language": language,
                "voice": settings.practice_tts_models[language],
                "stt_model": settings.deepgram_model,
                "transcript": transcript,
                "nonempty": bool(transcript.strip()),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        sources = [
            routes.source(
                "resume",
                "synthetic",
                "Acme original resume",
                "At Acme, I reduced PostgreSQL latency from 200 ms to 120 ms by adding an index.",
                "2026-11-20",
            )
        ]
        started = time.monotonic()
        answer = await routes.answer(svc, case["question"], [], sources)
        receipt["answers"].append(
            {
                "language": language,
                "answer": answer,
                "citation_present": "[S1]" in answer,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
    embedder = embed.make_embedder(settings, timeout_s=20)
    if embedder is None:
        receipt["embeddings"] = {"configured": False}
    else:
        try:
            started = time.monotonic()
            # Same fact across24 scripts, against English evidence and a distractor.
            texts = [c["evidence"] for c in corpus] + [
                "I reduced database latency from 200 ms to 120 ms.",
                "I took a vacation to visit the beach.",
            ]
            vectors = await embedder.embed(texts)
            receipt["embeddings"] = {
                "model": embedder.model_id,
                "batch_size": len(texts),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "languages": [
                    {
                        "language": c["language"],
                        "relevant_similarity": round(ask.cosine(vectors[i], vectors[-2]), 4),
                        "distractor_similarity": round(ask.cosine(vectors[i], vectors[-1]), 4),
                        "passes_threshold": ask.cosine(vectors[i], vectors[-2])
                        >= settings.ask_semantic_min_similarity,
                    }
                    for i, c in enumerate(corpus)
                ],
            }
        finally:
            close = getattr(getattr(embedder, "client", None), "close", None)
            if inspect.iscoroutinefunction(close):
                await close()
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Authorize the bounded existing-provider requests"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("Provider requests require explicit --live")
    corpus = json.loads(
        (Path(__file__).resolve().parents[1] / "tests/fixtures/call_languages.json").read_text(
            "utf-8"
        )
    )
    receipt = asyncio.run(run(corpus))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "receipt": str(args.output),
                "speech_checks": len(receipt["speech"]),
                "answer_checks": len(receipt["answers"]),
                "synthetic_only": True,
            }
        )
    )


if __name__ == "__main__":
    main()
