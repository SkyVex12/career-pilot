"""Explicit denominators and deterministic question-cluster uncertainty intervals."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_BOOTSTRAPS: ContextVar[dict[tuple[Any, ...], dict[str, Any]] | None] = ContextVar(
    "live_answer_bootstraps", default=None
)


@contextmanager
def bootstrap_scope() -> Generator[None]:
    """Reuse identical deterministic bootstrap inputs only within one analysis invocation."""
    token = _BOOTSTRAPS.set({}) if _BOOTSTRAPS.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _BOOTSTRAPS.reset(token)


def rate(successes: int, total: int) -> dict[str, Any]:
    if not 0 <= successes <= total:
        raise ValueError("Invalid rate denominator")
    if not total:
        return {"n": 0, "successes": 0, "rate": None, "wilson_95": None}
    z = 1.959963984540054
    proportion = successes / total
    scale = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / scale
    half = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total**2)) / scale
    return {
        "n": total,
        "successes": successes,
        "rate": proportion,
        "wilson_95": [max(0, center - half), min(1, center + half)],
        "caveat": "Descriptive interval; temporally correlated failures violate independence.",
    }


def paired_uncertainty(
    observations: list[tuple[str, float]], seed: int = 20261125, replicates: int = 1000
) -> dict[str, Any]:
    cache = _BOOTSTRAPS.get()
    if cache is None:
        return _paired_uncertainty(observations, seed, replicates)
    key = (tuple(observations), seed, replicates)
    if key not in cache:
        cache[key] = _paired_uncertainty(observations, seed, replicates)
    return deepcopy(cache[key])


def _paired_uncertainty(
    observations: list[tuple[str, float]], seed: int = 20261125, replicates: int = 1000
) -> dict[str, Any]:
    groups: dict[str, list[float]] = defaultdict(list)
    for question, difference in observations:
        if not math.isfinite(difference):
            raise ValueError("Bootstrap differences must be finite")
        groups[question].append(difference)
    result: dict[str, Any] = {
        "observed_pairs": len(observations),
        "unique_questions": len(groups),
        "method": "percentile bootstrap resampling question clusters; repeats stay together",
        "seed": seed,
        "replicates": replicates,
        "mean_difference_95": None,
        "median_difference_95": None,
        "caveat": "Conditional on observed matched pairs; not a correction for missingness.",
    }
    if len(groups) < 2:
        return result
    rng = random.Random(seed)
    samples = list(groups.values())
    means: list[float] = []
    medians: list[float] = []
    for _ in range(replicates):
        values = sorted(value for group in rng.choices(samples, k=len(samples)) for value in group)
        means.append(sum(values) / len(values))
        middle = len(values) // 2
        medians.append((values[middle] + values[~middle]) / 2)
    for name, values in (("mean_difference_95", means), ("median_difference_95", medians)):
        values.sort()
        interval: list[float] = []
        for quantile in (0.025, 0.975):
            index = (len(values) - 1) * quantile
            lo, hi = math.floor(index), math.ceil(index)
            interval.append(values[lo] + (values[hi] - values[lo]) * (index - lo))
        result[name] = interval
    return result
