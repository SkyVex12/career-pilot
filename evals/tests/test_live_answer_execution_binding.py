"""Reviewed execution controls must match before engine preflight or provider creation."""

from __future__ import annotations

import json
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals import live_answer_schedule as installed_schedule
from copilot_evals import live_answer_schedule as schedule
from copilot_evals.live_answer_eval import fingerprint

from copilot_gateway.config import Settings

REPO = Path(__file__).resolve().parents[2]


def source_identity(cases, settings, reps, seed, concurrency, timeout, provider_timeout=None):
    return {
        "schema": 1,
        "seed": seed,
        "concurrency": concurrency,
        "timeout_s": timeout,
        "provider_timeout_s": timeout if provider_timeout is None else provider_timeout,
        "code": {"evals/src/copilot_evals/live_answer_replay.py": "a" * 64},
        "git_head": "b" * 40,
        "git_diff_sha256": "c" * 64,
        "provider_configuration_sha256": replay.provider_configuration_fingerprint(settings),
    }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(
        installed_schedule, "explicit_case_blocks", schedule.explicit_case_blocks, raising=False
    )
    monkeypatch.setattr(replay, "experiment_identity", source_identity)
    monkeypatch.setattr(replay, "root_directory", lambda: REPO)

    def forbidden(*args, **kwargs):
        raise AssertionError("Provider creation forbidden")

    async def forbidden_preflight(*args, **kwargs):
        raise AssertionError("Unexpected engine preflight")

    monkeypatch.setattr(replay, "make_providers", forbidden)
    monkeypatch.setattr(replay, "preflight", forbidden_preflight)
    cases = replay.load_cases()[:2]
    for case in cases:
        case.update(answer_style="bullets", response_length="default")
    file = tmp_path / "cases.json"
    file.write_text(json.dumps(cases), encoding="utf-8")
    args = Namespace(
        cases=file,
        live=False,
        out=tmp_path / "unused",
        schedule_file=tmp_path / "schedule.json",
        reps=2,
        seed=20261125,
        architectures=["A", "D"],
        concurrency=2,
        timeout=90,
        max_provider_inflight=8,
        transport_failure_limit=3,
        skip_connectivity_check=False,
        max_cost=None,
    )
    settings = Settings(
        openai_detector_retry_tier=None, openai_fast_retry_tier=None, openai_deep_retry_tier=None
    )
    variants = replay.configured_conditions(args, settings, None)
    identity = source_identity(
        cases, settings, args.reps, args.seed, args.concurrency, args.timeout
    )
    execution = replay.schedule_run_identity(args, identity)
    manifest = {
        "schema": 2,
        "design": "execution-review",
        "case_sha256": fingerprint(cases),
        "condition_sha256": {
            v.label: fingerprint(v.identity(replay.CONFIG_FIELDS)) for v in variants.values()
        },
        "repetitions": 2,
        "execution": execution,
        "blocks": [
            {"case_id": c["id"], "rep": r, "conditions": ["A", "D"]}
            for r in range(2)
            for c in cases
        ],
    }
    args.schedule_file.write_text(json.dumps(manifest), encoding="utf-8")
    return args, cases, settings, variants, manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", 20),
        ("provider_timeout", 30),
        ("concurrency", 1),
        ("max_provider_inflight", 4),
        ("transport_failure_limit", 2),
        ("skip_connectivity_check", True),
        ("seed", 20261126),
        ("max_cost", 10),
    ],
)
async def test_changed_run_controls_fail_before_preflight(fixture, field, value):
    args, *_ = fixture
    setattr(args, field, value)
    with pytest.raises(ValueError, match="execution controls or source"):
        await replay.execute(args)
    assert not args.out.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", True),
        ("concurrency", True),
        ("max_provider_inflight", True),
        ("transport_failure_limit", True),
        ("skip_connectivity_check", 1),
        ("seed", True),
        ("max_cost", True),
        ("timeout", float("inf")),
        ("max_cost", float("nan")),
    ],
)
async def test_bool_and_nonfinite_controls_fail_before_preflight(fixture, field, value):
    args, *_ = fixture
    setattr(args, field, value)
    with pytest.raises(ValueError, match=r"Invalid explicit schedule|Invalid provider timeout"):
        await replay.execute(args)


@pytest.mark.parametrize("field", ["code", "git_head", "git_diff_sha256"])
async def test_changed_source_fails_before_preflight(fixture, monkeypatch, field):
    args, *_ = fixture

    def changed(*values):
        identity = source_identity(*values)
        identity[field] = (
            {"evals/src/copilot_evals/live_answer_replay.py": "d" * 64}
            if field == "code"
            else "d" * (40 if field == "git_head" else 64)
        )
        return identity

    monkeypatch.setattr(replay, "experiment_identity", changed)
    with pytest.raises(ValueError, match="execution controls or source"):
        await replay.execute(args)


@pytest.mark.parametrize(
    "field", ["concurrency", "max_provider_inflight", "transport_failure_limit", "seed"]
)
async def test_boolean_manifest_integer_is_not_equal_to_one(fixture, field):
    args, cases, settings, _, manifest = fixture
    setattr(args, field, 1)
    # concurrency/seed are read from the same base identity as the run.
    expected = replay.schedule_run_identity(
        args, source_identity(cases, settings, args.reps, args.seed, args.concurrency, args.timeout)
    )
    manifest["execution"] = deepcopy(expected)
    manifest["execution"][field] = True
    args.schedule_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="execution controls or source"):
        await replay.execute(args)


async def test_reviewed_binding_passes_preflight_without_any_provider(fixture, monkeypatch):
    args, *_ = fixture
    calls = []

    async def scripted_only(*values):
        calls.append("offline")

    monkeypatch.setattr(replay, "preflight", scripted_only)
    await replay.execute(args)
    assert calls == ["offline"]
    assert not args.out.exists()


def test_repository_source_fingerprint_reads_real_repo_not_private_copy(monkeypatch):
    monkeypatch.setattr(replay, "root_directory", lambda: REPO)
    args = Namespace(max_cost=None)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    identity = replay.experiment_identity([], settings, 1, 20261125, 2, 90)
    binding = replay.schedule_run_identity(args, identity)
    assert "evals/src/copilot_evals/live_answer_replay.py" in identity["code"]
    assert "evals/src/copilot_evals/live_answer_schedule.py" in identity["code"]
    assert all(not name.startswith("../") for name in identity["code"])
    assert binding["source"]["code_sha256"] == fingerprint(identity["code"])
    import hashlib

    source = REPO / "evals/src/copilot_evals/live_answer_replay.py"
    assert (
        identity["code"][source.relative_to(REPO).as_posix()]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_schedule_cannot_be_resolved_without_execution_binding(fixture):
    args, cases, _, variants, _ = fixture
    with pytest.raises(ValueError, match="requires bound execution"):
        replay.replay_blocks(args, cases, variants)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openai_base_url", "https://example.invalid/private?credential=fixture-only"),
        ("aws_region", "eu-west-2"),
        ("openai_http2", False),
        ("openai_http2_cancel_fix", False),
        ("llm_keepalive_s", 17),
        ("llm_connect_timeout_s", 5),
    ],
)
async def test_changed_provider_configuration_rejected_before_preflight(
    fixture, monkeypatch, field, value
):
    args, *_ = fixture

    def different_settings(*values, **kwargs):
        return Settings(*values, **kwargs).model_copy(update={field: value})

    monkeypatch.setattr(replay, "Settings", different_settings)
    with pytest.raises(ValueError, match=r"settings changed|execution controls or source"):
        await replay.execute(args)


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ],
)
async def test_sdk_transport_environment_is_bound_without_raw_values(fixture, monkeypatch, name):
    args, *_ = fixture
    value = "fixture-only-sensitive-value"
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="execution controls or source"):
        await replay.execute(args)
    assert value not in args.schedule_file.read_text("utf-8")


async def test_effective_os_proxy_configuration_is_bound(fixture, monkeypatch):
    args, *_ = fixture
    monkeypatch.setattr(
        replay, "getproxies", lambda: {"https": "https://user:fixture@proxy.invalid"}
    )
    with pytest.raises(ValueError, match="execution controls or source"):
        await replay.execute(args)
    assert "proxy.invalid" not in args.schedule_file.read_text("utf-8")


def test_provider_configuration_identity_contains_no_raw_endpoint_or_key(monkeypatch):
    monkeypatch.setattr(
        replay, "getproxies", lambda: {"https": "https://proxy-user:private@proxy.invalid"}
    )
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        openai_base_url="https://user:private@endpoint.invalid/v1?key=secret",
    )
    identity = replay.provider_configuration_fingerprint(settings)
    assert len(identity) == 64
    assert set(identity) <= set("0123456789abcdef")
    assert "private" not in identity
    assert "endpoint.invalid" not in identity


def adaptive_expert_profile():
    return {
        "model": "authored-expert",
        "reasoning": "low",
        "service_tier": "fast",
        "retry_tier": "fast",
        "first_token_timeout_ms": 20000,
        "hedge_ms": None,
    }


@pytest.mark.parametrize("field", ["adaptive_expert_route", "adaptive_technical_legacy"])
async def test_explicit_product_policy_drift_fails_before_provider_or_preflight(fixture, field):
    args, *_ = fixture
    # Product defaults are intentionally excluded. Mutating an explicit condition still
    # invalidates the reviewed schedule before any provider or engine work.
    args.variants = args.cases.parent / "changed-variants.json"
    args.variants.write_text(
        json.dumps(
            {
                "schema": 1,
                "variants": [
                    {"label": "A", "architecture": "legacy_dual"},
                    {
                        "label": "D",
                        "architecture": "adaptive_on_demand",
                        "settings": {
                            field: adaptive_expert_profile()
                            if field == "adaptive_expert_route"
                            else True,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="request condition settings changed"):
        await replay.execute(args)
    assert not args.out.exists()


def test_adaptive_expert_product_profile_roundtrips_into_variant_identity(tmp_path):
    from copilot_evals.live_answer_variants import load_variants

    file = tmp_path / "variants.json"
    profile = adaptive_expert_profile()
    file.write_text(
        json.dumps(
            {
                "schema": 1,
                "variants": [
                    {
                        "label": "B",
                        "architecture": "adaptive",
                        "settings": {"adaptive_expert_route": profile},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    base = Settings(_env_file=None, adaptive_expert_route=None)  # pyright: ignore[reportCallIssue]
    loaded = load_variants(file, base, REPO)["B"]
    assert loaded.settings.adaptive_expert_route is not None
    assert loaded.settings.adaptive_expert_route.model_dump() == profile
    assert loaded.identity(replay.CONFIG_FIELDS)["settings"]["adaptive_expert_route"] == profile
    assert base.adaptive_expert_route is None


@pytest.mark.parametrize(("name", "value"), [("expert_tier", "fast"), ("expert_route_limit", 1)])
@pytest.mark.parametrize("entry", ["constructor", "manifest"])
def test_adaptive_expert_profile_rejects_eval_expert_overrides(tmp_path, name, value, entry):
    from copilot_evals.live_answer_variants import Variant, load_variants

    base = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    settings = Settings.model_validate(
        {
            **base.model_dump(),
            "adaptive_expert_route": adaptive_expert_profile(),
        }
    )
    if entry == "constructor":
        with pytest.raises(ValueError, match="cannot use eval expert route overrides"):
            Variant("B", "adaptive", settings, **{name: value})
    else:
        file = tmp_path / "variants.json"
        file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "variants": [
                        {
                            "label": "B",
                            "architecture": "adaptive",
                            "settings": {"adaptive_expert_route": adaptive_expert_profile()},
                            name: value,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="cannot use eval expert route overrides"):
            load_variants(file, settings, REPO)


def test_adaptive_expert_unpriced_route_cannot_escape_pre_dispatch_check():
    from typing import Any, cast

    from copilot_evals.live_answer_variants import validate_route_prices

    from copilot_core.llm import ModelRouter, Route
    from copilot_core.pricing import ModelPrice

    settings = Settings(_env_file=None, model_prices={})  # pyright: ignore[reportCallIssue]
    router = ModelRouter(
        {
            "adaptive_expert": [
                Route(cast(Any, object()), "authored-expert", {"service_tier": "fast"}),
            ]
        },
        first_token_timeout_s=1,
    )
    with pytest.raises(ValueError, match="Model/tier rate missing"):
        validate_route_prices(router, settings)
    priced = settings.model_copy(
        update={
            "model_prices": {"authored-expert": ModelPrice(input=1, cached=0.1, output=2)},
            "tier_price_multipliers": {"standard": 1, "authored-expert:fast": 2},
        }
    )
    validate_route_prices(router, priced)
