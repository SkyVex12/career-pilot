"""Explicit eval-only model/prompt variants, with no global production prompt mutation."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, cast

from copilot_core import prompts
from copilot_core.llm import LLMRequest, ModelRouter
from copilot_core.pricing import ModelPrice, cost_usd, openai_cache_write_pricing
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question
from copilot_gateway.config import Settings

SETTINGS_FIELDS = {
    "openai_fast_model",
    "openai_deep_model",
    "openai_fast_reasoning",
    "openai_deep_reasoning",
    "openai_first_token_timeout_ms",
    "openai_deep_first_token_timeout_ms",
    "openai_fast_retry_tier",
    "openai_deep_retry_tier",
    "fast_hedge_ms",
    "deep_hedge_ms",
    "fast_pass_max_tokens",
    "deep_pass_max_tokens",
    "adaptive_failure_recovery",
    "adaptive_max_tokens",
    "adaptive_stream_idle_timeout_s",
    "adaptive_stream_total_timeout_s",
    "openai_reasoning_headroom_tokens",
}
VARIANT_FIELDS = {
    "label",
    "architecture",
    "settings",
    "prompt_commit",
    "prompt_file",
    "fast_tier",
    "expert_tier",
    "fast_route_limit",
    "expert_route_limit",
    "synthetic_fault",
}
STRATEGIES = {"legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"}
SOURCE_PATH = "packages/core/src/copilot_core/prompts.py"


def load_rate_card(settings: Settings, path: Path) -> tuple[Settings, dict[str, Any]]:
    """Explicit dated rates augment configured rates; source attestation stays visible."""
    raw = path.read_bytes()
    data = json.loads(raw)
    if set(data) != {"schema", "verified_at", "sources", "model_prices", "tier_price_multipliers"}:
        raise ValueError("Rate card requires schema, date, sources, models and tier multipliers")
    if data["schema"] != 1:
        raise ValueError("Rate card requires schema 1")
    date.fromisoformat(data["verified_at"])
    if (
        not isinstance(data["sources"], list)
        or not data["sources"]
        or any(
            not isinstance(source, str) or not source.startswith("https://")
            for source in data["sources"]
        )
    ):
        raise ValueError("Rate card must record HTTPS source URLs")
    prices = {key: ModelPrice.model_validate(value) for key, value in data["model_prices"].items()}
    tiers = data["tier_price_multipliers"]
    if not prices or not isinstance(tiers, dict):
        raise ValueError("Rate card must include model rates and a tier mapping")
    tiers = cast(dict[str, Any], tiers)
    if any(
        not isinstance(key, str)
        or not key.strip()
        or any(not math.isfinite(value) or value < 0 for value in price.model_dump().values())
        for key, price in prices.items()
    ) or any(
        not key.strip() or type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for key, value in tiers.items()
    ):
        raise ValueError("Rates must be finite nonnegative numbers and tiers must be positive")
    updated = Settings.model_validate(
        {
            **settings.model_dump(),
            "model_prices": {**settings.model_prices, **prices},
            "tier_price_multipliers": {**settings.tier_price_multipliers, **tiers},
        }
    )
    return updated, {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        **data,
        "verification": "Caller-supplied source record; loader does not browse or verify prices",
    }


def validate_route_prices(router: ModelRouter, settings: Settings) -> None:
    """Reject unpriced eval routes before any model request, including possible retries."""
    for job in ("fast", "deep_light", "deep", "answer_recovery"):
        for route in router.routes.get(job, []):
            tier = route.params.get("service_tier") or "standard"
            if tier in ("auto", "default"):
                tier = "standard"
            value = cost_usd(
                route.model,
                str(tier),
                1,
                0,
                1,
                settings.model_prices,
                settings.tier_price_multipliers,
                **({"cache_write_tokens": 0} if openai_cache_write_pricing(route.model) else {}),
            )
            if value is None:
                raise ValueError("Model/tier rate missing; refusing to start the live schedule")


def adaptive_constant(source: str) -> str:
    """Read only literal strings, concatenation and the two allowed constant references."""
    assignments = {
        node.targets[0].id: node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"_RULES", "ADAPTIVE_SYSTEM"}
    }

    def literal(node: ast.AST, visiting: set[str]) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left, visiting) + literal(node.right, visiting)
        if isinstance(node, ast.Name) and node.id in assignments and node.id not in visiting:
            return literal(assignments[node.id], visiting | {node.id})
        raise ValueError("Pinned prompt must contain only allowed literal string expressions")

    if "ADAPTIVE_SYSTEM" not in assignments:
        raise ValueError("Pinned source has no adaptive prompt")
    return literal(assignments["ADAPTIVE_SYSTEM"], {"ADAPTIVE_SYSTEM"})


def pinned_prompt(root: Path, ref: str) -> tuple[str, dict[str, str], str]:
    if not re.fullmatch(r"[a-fA-F0-9]{7,40}", ref):
        raise ValueError("Prompt commit must be an explicit Git commit SHA")
    commit = (
        subprocess.run(
            ["git", "rev-parse", "--verify", ref + "^{commit}"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    source = subprocess.run(
        ["git", "show", commit + ":" + SOURCE_PATH],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    rules = adaptive_constant(source.decode("utf-8"))
    return (
        rules,
        {
            "commit": commit,
            "path": SOURCE_PATH,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
            "scope": "adaptive instructions only; current depth, routing, evidence and formatting",
        },
        source.decode("utf-8"),
    )


def file_prompt(path: Path) -> tuple[str, dict[str, str], str]:
    """Read a private draft as inert AST literals, never import or execute its source."""
    source = path.read_bytes()
    if len(source) > 262144:
        raise ValueError("Draft prompt file exceeds 256 KiB")
    rules = adaptive_constant(source.decode("utf-8"))
    if not rules.strip():
        raise ValueError("Draft prompt must not be empty")
    return (
        rules,
        {
            "version": "explicit-private-draft",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
            "scope": "adaptive instructions only; current depth, routing, evidence and formatting",
        },
        source.decode("utf-8"),
    )


@dataclass(frozen=True)
class SyntheticFault:
    """Explicit experiment fault, before provider admission or any billed request."""

    kind: str = "connect_before_dispatch"
    delay_ms: int = 0

    @classmethod
    def parse(cls, value: Any) -> SyntheticFault:
        if not isinstance(value, dict):
            raise ValueError("Synthetic fault requires a mapping")
        value = cast(dict[str, Any], value)
        if set(value) - {"kind", "delay_ms"}:
            raise ValueError("Synthetic fault requires only kind and optional delay_ms")
        if value.get("kind") != "connect_before_dispatch":
            raise ValueError("Only explicit pre-dispatch connection injection is supported")
        delay = value.get("delay_ms", 0)
        if type(delay) is not int or not 0 <= delay <= 250:
            raise ValueError("Synthetic fault delay must be an integer from zero to 250 ms")
        return cls(delay_ms=delay)


@dataclass
class Variant:
    label: str
    architecture: str
    settings: Settings
    prompt_rules: str | None = None
    prompt_identity: dict[str, str] | None = None
    prompt_sources: dict[str, str] | None = None
    fast_tier: str | None = None
    expert_tier: str | None = None
    fast_route_limit: int | None = None
    expert_route_limit: int | None = None
    synthetic_fault: SyntheticFault | None = None

    def identity(self, config_fields: set[str]) -> dict[str, Any]:
        return {
            "label": self.label,
            "architecture": self.architecture,
            "settings": self.settings.model_dump(mode="json", include=config_fields),
            "prompt": self.prompt_identity
            or {
                "version": "current",
                "rules_sha256": hashlib.sha256(prompts.ADAPTIVE_SYSTEM.encode()).hexdigest(),
            },
            "fast_tier": self.fast_tier,
            "expert_tier": self.expert_tier,
            "fast_route_limit": self.fast_route_limit,
            "expert_route_limit": self.expert_route_limit,
            "synthetic_fault": asdict(self.synthetic_fault) if self.synthetic_fault else None,
        }

    def limit_routes(self, router: ModelRouter) -> None:
        for job, limit in (
            ("fast", self.fast_route_limit),
            ("deep_light", self.fast_route_limit),
            ("deep", self.expert_route_limit),
        ):
            if limit is not None:
                router.routes = {**router.routes, job: list(router.routes[job])[:limit]}


def load_variants(path: Path, settings: Settings, root: Path) -> dict[str, Variant]:
    manifest = json.loads(path.read_text("utf-8"))
    if set(manifest) != {"schema", "variants"} or manifest["schema"] != 1:
        raise ValueError("Variant manifest requires schema 1 and variants")
    if not isinstance(manifest["variants"], list) or not 1 <= len(manifest["variants"]) <= 32:
        raise ValueError("Select one to 32 explicit variants per finite schedule")
    variants: dict[str, Variant] = {}
    for item in manifest["variants"]:
        if set(item) - VARIANT_FIELDS:
            raise ValueError("Unknown variant field")
        label = item["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", label):
            raise ValueError("Variant label must be a short distinct identifier without colons")
        if label in variants or item["architecture"] not in STRATEGIES:
            raise ValueError("Duplicate variant or unknown architecture")
        overrides = item.get("settings", {})
        if (
            not isinstance(overrides, dict)
            or set(cast(dict[str, Any], overrides)) - SETTINGS_FIELDS
        ):
            raise ValueError("Unsupported variant settings")
        effective = Settings.model_validate(
            {**settings.model_dump(), **overrides, "answer_architecture": item["architecture"]}
        )
        options: dict[str, Any] = {}
        if item.get("synthetic_fault") is not None:
            if item["architecture"] != "adaptive_on_demand":
                raise ValueError("Recovery fault diagnostics require adaptive_on_demand")
            options["synthetic_fault"] = SyntheticFault.parse(item["synthetic_fault"])
        for name in ("fast_tier", "expert_tier"):
            value = item.get(name)
            if value not in (None, "default", "priority", "fast", "flex"):
                raise ValueError("Unknown variant tier")
            options[name] = value
        for name in ("fast_route_limit", "expert_route_limit"):
            value = item.get(name)
            if value is not None and (type(value) is not int or not 1 <= value <= 4):
                raise ValueError("Route limit must retain between one and four existing routes")
            options[name] = value
        if item.get("prompt_file") and item.get("prompt_commit"):
            raise ValueError("Select either a pinned prompt commit or a private draft file")
        rules, identity, source = (
            file_prompt((path.parent / item["prompt_file"]).resolve())
            if item.get("prompt_file")
            else pinned_prompt(root, item["prompt_commit"])
            if item.get("prompt_commit")
            else (None, None, None)
        )
        if rules is not None and item["architecture"] == "legacy_dual":
            raise ValueError("Adaptive prompt ablation cannot modify the legacy baseline")
        variants[label] = Variant(
            label,
            item["architecture"],
            effective,
            rules,
            identity,
            prompt_sources={"source": source, "rules": rules}
            if source is not None and rules is not None
            else None,
            **options,
        )
    return variants


class EvaluationEngine(AnswerEngine):
    """Each instance may substitute a pinned instruction block; shared globals stay intact."""

    def __init__(self, host: AnswerHost, rules: str):
        super().__init__(host)
        self.rules = rules

    def _base_request(self, q: Question, pass_: str, memory: list[str] | None = None) -> LLMRequest:
        request = super()._base_request(q, pass_, memory)
        if (
            self.strategy(q) == "legacy_dual"
            or q.screen is not None
            or self.open_problem() is not None
        ):
            return request
        # Eval-only substitution intentionally calls the current context wrapper.
        replacement = prompts._system(  # pyright: ignore[reportPrivateUsage]
            self.rules, self.host.prompt_context(), strict=True
        )
        return replace(request, system=[replacement[0], *request.system[1:]])
