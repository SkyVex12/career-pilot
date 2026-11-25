"""Counterbalanced blocks and conservative circuit breaking for live replay."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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


def has_response_progress(result: dict[str, Any], attempts: list[dict[str, Any]]) -> bool:
    """Operational progress, not a judgment of answer quality or grammatical usefulness."""
    return (
        not result.get("failure")
        or any(
            row.get("status") == "completed"
            or (isinstance(row.get("streamed_chars"), int) and row["streamed_chars"] > 0)
            for row in attempts
        )
        or any(
            entry.get("kind") == "provider_attempt"
            and entry.get("status") in {"first_chunk", "completed"}
            for entry in result.get("metadata_logs", [])
        )
        or any(
            event.get("message", {}).get("type") == "answer.delta"
            and bool(event["message"].get("text"))
            for event in result.get("events", [])
        )
    )


@dataclass
class FailureCircuit:
    threshold: int = 3
    consecutive: int = 0
    reason: str | None = None
    zero_output_timeout_threshold: int = 8
    zero_output_timeouts: int = 0
    last_failure_kind: str | None = None

    def __post_init__(self) -> None:
        if type(self.threshold) is not int or not 1 <= self.threshold <= 10:
            raise ValueError("Transport circuit threshold must be between one and ten")
        if (
            type(self.zero_output_timeout_threshold) is not int
            or not 1 <= self.zero_output_timeout_threshold <= 100
        ):
            raise ValueError("Zero-output timeout threshold must be between one and 100")

    def observe(self, result: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        if self.reason is not None or result.get("expected_injected_failure"):
            return
        kind = classify_failure(result, attempts)
        self.last_failure_kind = kind
        # A single intentionally short route deadline is not an outage declaration.
        # Repeated zero-output deadlines are a separate, money-independent stop policy.
        if has_response_progress(result, attempts):
            self.zero_output_timeouts = 0
        elif kind in {"first_token_timeout", "provider_timeout"} or (
            result.get("failure") == "TimeoutError"
            and any(row.get("provider_invoked") is True for row in attempts)
        ):
            self.zero_output_timeouts += 1
            if self.zero_output_timeouts >= self.zero_output_timeout_threshold:
                self.reason = "zero_output_timeout_streak"
        # Other failed outcomes neither prove delivery progress nor add timeout evidence.
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
            if self.consecutive >= self.threshold and self.reason is None:
                self.reason = kind
        else:
            self.consecutive = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport_threshold": self.threshold,
            "transport_consecutive": self.consecutive,
            "zero_output_timeout_threshold": self.zero_output_timeout_threshold,
            "zero_output_timeouts_since_progress": self.zero_output_timeouts,
            "last_failure_kind": self.last_failure_kind,
            "reason": self.reason,
            "scope": "Trial completion order; operational delivery guard independent of budget",
            "rationale": (
                "Repeated first-token/provider deadlines with no observed response progress; "
                "not a claim of provider outage or answer quality. Text/terminal success resets; "
                "other failures are neutral. Partial answers do not add timeout evidence."
            ),
        }


def explicit_case_blocks(
    path: Path,
    cases: list[dict[str, Any]],
    conditions: dict[str, str],
    condition_identities: dict[str, dict[str, Any]],
    reps: int,
    execution: dict[str, Any],
) -> tuple[list[list[tuple[dict[str, Any], str, int]]], dict[str, Any]]:
    """Bind a finite predeclared order to exact effective inputs and request settings.

    A worker executes each block sequentially. Other case blocks can interleave;
    this order does not claim balanced realized provider timing or cache history.
    """
    from copilot_evals.live_answer_eval import fingerprint

    if type(reps) is not int or not 1 <= reps <= 10:
        raise ValueError("Explicit schedule requires one to ten repetitions")
    if (
        not conditions
        or len(set(conditions.values())) != len(conditions)
        or set(condition_identities) != set(conditions.values())
    ):
        raise ValueError("Explicit schedule requires distinct bound conditions")
    if path.stat().st_size > 5_000_000:
        raise ValueError("Explicit schedule exceeds the finite input size limit")
    raw = path.read_bytes()
    if len(raw) > 5_000_000:
        raise ValueError("Explicit schedule exceeds the finite input size limit")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("Explicit schedule must be an object")
    manifest = cast(dict[str, Any], decoded)
    fields = {
        "schema",
        "design",
        "case_sha256",
        "condition_sha256",
        "repetitions",
        "execution",
        "blocks",
    }
    if set(manifest) != fields:
        raise ValueError("Explicit schedule has unrecognized or missing fields")
    if type(manifest["schema"]) is not int or manifest["schema"] != 2:
        raise ValueError("Unsupported explicit schedule schema")
    if (
        not isinstance(manifest["design"], str)
        or not manifest["design"].strip()
        or len(manifest["design"]) > 200
    ):
        raise ValueError("Explicit schedule requires a bounded design label")
    if type(manifest["repetitions"]) is not int or manifest["repetitions"] != reps:
        raise ValueError("Explicit schedule repetitions differ from run settings")
    if manifest["case_sha256"] != fingerprint(cases):
        raise ValueError("Explicit schedule effective case inputs changed")
    expected_conditions = {
        label: fingerprint(condition_identities[condition])
        for label, condition in conditions.items()
    }
    if manifest["condition_sha256"] != expected_conditions:
        raise ValueError("Explicit schedule request condition settings changed")
    # Canonical JSON comparison also distinguishes bool from int: True is not 1.
    if not isinstance(manifest["execution"], dict) or fingerprint(
        manifest["execution"]
    ) != fingerprint(execution):
        raise ValueError("Explicit schedule execution controls or source changed")
    by_id = {case["id"]: case for case in cases}
    if not by_id or len(by_id) != len(cases):
        raise ValueError("Explicit schedule requires distinct case identities")
    if not isinstance(manifest["blocks"], list):
        raise ValueError("Explicit schedule blocks must be a list")
    rows = cast(list[Any], manifest["blocks"])
    if len(rows) != len(cases) * reps:
        raise ValueError("Explicit schedule must cover every case and repetition")
    seen: set[tuple[str, int]] = set()
    blocks: list[list[tuple[dict[str, Any], str, int]]] = []
    for candidate in rows:
        if not isinstance(candidate, dict):
            raise ValueError("Explicit schedule block must be an object")
        row = cast(dict[str, Any], candidate)
        if set(row) != {"case_id", "rep", "conditions"}:
            raise ValueError("Explicit schedule block has invalid fields")
        identifier, rep, labels = row["case_id"], row["rep"], row["conditions"]
        if (
            not isinstance(identifier, str)
            or identifier not in by_id
            or type(rep) is not int
            or not 0 <= rep < reps
            or (identifier, rep) in seen
        ):
            raise ValueError("Explicit schedule has unknown or duplicate case/repetition")
        if not isinstance(labels, list):
            raise ValueError("Explicit schedule conditions must be a list")
        labels = cast(list[Any], labels)
        if (
            any(not isinstance(label, str) for label in labels)
            or len(labels) != len(conditions)
            or set(labels) != set(conditions)
        ):
            raise ValueError("Explicit schedule block must contain each condition exactly once")
        seen.add((identifier, rep))
        blocks.append([(by_id[identifier], conditions[label], rep) for label in labels])
    if seen != {(key, rep) for key in by_id for rep in range(reps)}:
        raise ValueError("Explicit schedule omits planned case/repetition cells")
    return blocks, {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_sha256": fingerprint(manifest),
        "manifest": manifest,
        "seed_effect": "Run seed does not alter the explicit block order",
        "execution": "Sequential conditions per case block; bounded concurrent case workers",
    }
