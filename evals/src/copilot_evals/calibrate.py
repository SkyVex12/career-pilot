"""Calibrate prepared-answer matching for an embedding model.

    uv run copilot-calibrate-prepared            # the configured embedder (.env)
    uv run copilot-calibrate-prepared --json out.json

Embeds the prepared questions of evals/bench/profile.toml and the probes of
evals/prepared/calibration.toml (paraphrases that should match, and questions that must not)
the way the gateway does, then scores every (threshold, margin) pair: a probe matches when its
best similarity reaches the threshold and beats the runner-up by the margin. A wrong match
(a negative matching anything, or a paraphrase matching another prepared answer) shows the
user points for a different question, so the recommendation is the pair with the best recall
among those with no wrong match, with a safety step added to the threshold.
"""

import argparse
import asyncio
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from copilot_evals.benchdata import load_profile

CALIBRATION = Path(__file__).resolve().parents[2] / "prepared" / "calibration.toml"
SAFETY = 0.03  # added to the lowest safe threshold: unseen wording scores a little higher


@dataclass(frozen=True)
class Probe:
    text: str
    expected: str | None  # the prepared answer it paraphrases; None for a negative
    best: str
    best_sim: float
    runner_up: float

    @property
    def margin(self) -> float:
        return self.best_sim - self.runner_up


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def evaluate(probes: list[Probe], threshold: float, margin: float) -> dict[str, int]:
    hits = wrong = 0
    for p in probes:
        if p.best_sim < threshold or p.margin < margin:
            continue
        if p.expected is not None and p.best == p.expected:
            hits += 1
        else:
            wrong += 1
    return {"hits": hits, "wrong": wrong}


def recommend(probes: list[Probe]) -> dict[str, Any]:
    positives = sum(1 for p in probes if p.expected is not None)
    best: dict[str, Any] | None = None
    for margin_step in range(0, 31):  # margin 0.00 to 0.30
        margin = margin_step / 100
        for t_step in range(30, 100):  # threshold 0.30 to 0.99
            threshold = t_step / 100
            r = evaluate(probes, threshold, margin)
            if r["wrong"]:
                continue
            # The lowest safe threshold at this margin; candidates differ only in margin.
            safe = round(min(threshold + SAFETY, 0.99), 2)
            at_safe = evaluate(probes, safe, margin)
            cand = {
                "min_similarity": safe,
                "min_margin": margin,
                "recall": round(at_safe["hits"] / positives, 3),
                "lowest_safe_threshold": threshold,
            }
            if (
                best is None
                or cand["recall"] > best["recall"]
                or (cand["recall"] == best["recall"] and margin < best["min_margin"])
            ):
                best = cand
            break
    assert best is not None
    return best


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Calibrate prepared-answer matching.")
    p.add_argument("--json", type=Path, help="write probes and the recommendation here")
    args = p.parse_args(argv)

    from copilot_core.config import CoreSettings
    from copilot_core.dotenv import load_dotenv
    from copilot_core.embed import make_embedder

    load_dotenv()
    embedder = make_embedder(CoreSettings())
    if embedder is None:
        raise SystemExit("no embedder configured (COPILOT_EMBEDDER, model providers)")
    profile = load_profile()
    data = tomllib.loads(CALIBRATION.read_text("utf-8"))
    positives = cast(dict[str, list[str]], data["positives"])
    negatives = cast(list[str], data["negatives"]["questions"])
    unknown = set(positives) - {a.id for a in profile.prepared}
    if unknown:
        raise SystemExit(f"calibration names unknown prepared answers: {sorted(unknown)}")
    probe_texts = [(t, pid) for pid, ts in positives.items() for t in ts]
    probe_texts += [(t, None) for t in negatives]
    vectors = await embedder.embed(
        [a.question for a in profile.prepared] + [t for t, _ in probe_texts]
    )
    prepared: list[tuple[str, list[float]]] = list(
        zip([a.id for a in profile.prepared], vectors, strict=False)
    )
    probes: list[Probe] = []
    for (text, expected), vec in zip(probe_texts, vectors[len(prepared) :], strict=True):
        sims = sorted(((cosine(vec, pv), pid) for pid, pv in prepared), reverse=True)
        probes.append(Probe(text, expected, sims[0][1], sims[0][0], sims[1][0]))

    pos = [x for x in probes if x.expected is not None]
    neg = [x for x in probes if x.expected is None]

    def span(xs: list[Probe]) -> str:
        return f"{min(x.best_sim for x in xs):.3f} to {max(x.best_sim for x in xs):.3f}"

    right = sum(x.best == x.expected for x in pos)
    print(f"{embedder.model_id}: {len(prepared)} prepared, {len(pos)} paraphrases, "
          f"{len(neg)} negatives")  # fmt: skip
    print(f"paraphrases: best similarity {span(pos)}; top-1 right on {right}/{len(pos)}")
    print(f"negatives:   best similarity {span(neg)}")
    print("\nhighest-scoring negatives:")
    for x in sorted(neg, key=lambda x: -x.best_sim)[:5]:
        print(f"  {x.best_sim:.3f} (margin {x.margin:.3f}) -> {x.best}: {x.text}")
    print("\nlowest-scoring paraphrases:")
    for x in sorted(pos, key=lambda x: x.best_sim)[:5]:
        flag = "" if x.best == x.expected else f" WRONG TOP-1 ({x.best})"
        print(f"  {x.best_sim:.3f} (margin {x.margin:.3f}) {x.expected}{flag}: {x.text}")
    print("\nthreshold / margin: hits of", len(pos), "and wrong matches")
    for t in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.85):
        cells: list[str] = []
        for m in (0.0, 0.05, 0.10, 0.15):
            r = evaluate(probes, t, m)
            cells.append(f"m{m:.2f}: {r['hits']:>2}/{r['wrong']}")
        print(f"  t{t:.2f}  " + "  ".join(cells))
    rec = recommend(probes)
    print(f"\nrecommended for {embedder.model_id}: {json.dumps(rec)}")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "model": embedder.model_id,
                    "recommended": rec,
                    "probes": [x.__dict__ | {"margin": x.margin} for x in probes],
                },
                indent=2,
            ),
            "utf-8",
        )
    return 0


def run() -> None:
    sys.exit(asyncio.run(main()))
