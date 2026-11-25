"""Counterbalanced blocks and conservative circuit breaking for live replay."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

TRANSPORT_CAUSES = {"ConnectTimeout", "ConnectError", "PoolTimeout", "ReadError", "WriteError"}


def case_blocks(
    cases: list[dict[str, Any]], strategies: list[str], reps: int, seed: int
) -> list[list[tuple[dict[str, Any], str, int]]]:
    """Each question sees every condition consecutively; order rotates across cases/reps."""
    rng = random.Random(seed)
    order = list(range(len(cases)))
    rng.shuffle(order)
    rotation = list(strategies)
    rng.shuffle(rotation)
    blocks: list[list[tuple[dict[str, Any], str, int]]] = []
    for rep in range(reps):
        indices = list(enumerate(order))
        rng.shuffle(indices)
        for rank, index in indices:
            start = (rank + rep) % len(rotation)
            sequence = rotation[start:] + rotation[:start]
            blocks.append([(cases[index], strategy, rep) for strategy in sequence])
    return blocks


def classify_failure(result: dict[str, Any], attempts: list[dict[str, Any]]) -> str | None:
    if not result.get("failure"):
        return None
    lifecycle = [
        entry
        for entry in result.get("metadata_logs", [])
        if entry.get("kind") == "provider_attempt"
    ]
    causes = {cause for attempt in attempts for cause in attempt.get("error_types", [])}
    causes.update(cause for entry in lifecycle for cause in entry.get("cause_types", []))
    categories = {entry.get("error_category") for entry in lifecycle}

    states = {attempt.get("status") for attempt in attempts}
    if causes & {"AuthenticationError", "PermissionDeniedError"}:
        return "authentication"
    if "RateLimitError" in causes:
        return "rate_limit"
    if causes & {"BadRequestError", "NotFoundError", "UnprocessableEntityError"}:
        return "request_configuration"
    if causes & TRANSPORT_CAUSES:
        return "transport"
    if "SSLError" in causes and "APIConnectionError" in causes:
        return "tls_connection"
    if "connection_error" in categories:
        return "connection_unknown"
    if "first_token_timeout" in categories:
        return "first_token_timeout"
    if "APITimeoutError" in causes:
        return "provider_timeout"
    if states and states <= {"CancelledError", "GeneratorExit"}:
        return "timeout_or_cancellation"
    if "InternalServerError" in causes:
        return "provider_server"
    return "provider_or_engine"


@dataclass
class FailureCircuit:
    threshold: int = 3
    consecutive: int = 0
    reason: str | None = None

    def observe(self, result: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        kind = classify_failure(result, attempts)
        if kind in {"authentication", "request_configuration"}:
            self.reason = kind
        elif kind in {
            "transport",
            "tls_connection",
            "connection_unknown",
            "rate_limit",
            "provider_server",
        } and not any(attempt.get("status") == "completed" for attempt in attempts):
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self.reason = kind
        else:
            self.consecutive = 0
