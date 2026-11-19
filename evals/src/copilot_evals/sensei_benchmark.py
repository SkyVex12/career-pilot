"""Run the private Sensei comparison with one command; sources stay outside git.

python -m copilot_evals.sensei_benchmark --export PATH --private-dir PATH --year 2026
The private directory must contain resume.txt and jd.txt. Completed phases are cached.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from copilot_evals.sensei import inspect_export, parse_turns
from copilot_evals.sensei_runtime import prepare, private_directory, replay, save


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_inputs(directory: Path, export: Path, year: int) -> None:
    """Reject stale caches when any source changes; never persist source paths in git."""
    manifest = {
        "schema": 1,
        "export_sha256": fingerprint(export),
        "resume_sha256": fingerprint(directory / "resume.txt"),
        "jd_sha256": fingerprint(directory / "jd.txt"),
        "year": year,
    }
    target = directory / "inputs.json"
    if target.exists() and json.loads(target.read_text("utf-8")) != manifest:
        raise ValueError("Sources changed; use a fresh private directory")
    if not target.exists():
        # Safely adopt an earlier checkpoint only when its actual source data matches.
        if (directory / "turns.json").exists():
            cached = json.loads((directory / "turns.json").read_text("utf-8"))
            if cached != load_turns(export, year):
                raise ValueError("Export differs from cached turns")
        if (directory / "profile.json").exists():
            profile = json.loads((directory / "profile.json").read_text("utf-8"))
            if any(
                profile[key] != (directory / filename).read_text("utf-8")
                for key, filename in (("resume_source", "resume.txt"), ("jd_source", "jd.txt"))
            ):
                raise ValueError("Profile differs from supplied evidence")
        save(target, manifest)


def reuse_profile(directory: Path, source: Path) -> None:
    source = source.resolve()
    private_directory(source)
    for name in ("resume.txt", "jd.txt", "profile.json", "extracted.json", "annotations.json"):
        target = directory / name
        origin = source / name
        if target.exists() and fingerprint(target) != fingerprint(origin):
            raise ValueError("Refusing to overwrite different profile evidence")
        if not target.exists():
            shutil.copyfile(origin, target)


def replay_identity(directory: Path, speed: float) -> None:
    from copilot_gateway.config import Settings

    root = Path(__file__).resolve().parents[3]
    settings = Settings()
    # Explicit scalar allowlist: credentials, endpoints and database URLs never enter it.
    fields = [
        "llm_providers",
        "openai_fast_model",
        "openai_deep_model",
        "openai_detector_model",
        "openai_summary_model",
        "openai_fast_reasoning",
        "openai_deep_reasoning",
        "openai_deep_light_model",
        "openai_fast_service_tier",
        "openai_deep_service_tier",
        "embedding_provider",
        "embedding_model",
        "llm_first_token_timeout_s",
        "llm_total_timeout_s",
        "fast_pass_max_tokens",
        "deep_pass_max_tokens",
        "silence_timer_ms",
        "speculative_start_ms",
        "speculative_hold",
        "speculative_min_words",
        "group_hold_min_words",
        "early_decision_interval_ms",
        "early_decision_all_modes",
        "held_keep_added_words",
        "detector_max_tokens",
        "detector_api",
        "decisions_model",
        "decisions_hedge_ms",
        "decisions_timeout_ms",
        "detector_hedge_ms",
        "first_show_min_words",
        "fast_hedge_ms",
        "deep_hedge_ms",
        "openai_fast_retry_tier",
        "openai_deep_retry_tier",
        "name_address_grace_ms",
        "prepared_answer_thresholds",
        "prepared_answer_min_similarity",
        "fast_pass_priority",
        "deep_pass_auto",
        "deep_routing",
        "deep_pass_auto_types",
        "rerank_keep",
        "memory_fast_wait_ms",
        "memory_deep_wait_ms",
        "memory_fast_hits",
        "pack_render_timeout_ms",
        "first_token_timeout_ms",
        "conversation_budget_tokens",
        "conversation_summary_tokens",
        "conversation_answers_tokens",
        "conversation_summary_chunk_tokens",
        "conversation_summary_retry_s",
        "detector_context_tokens",
        "final_wait_max_ms",
        "follow_user",
        "follow_min_words",
        "follow_window_ms",
        "continuation_gap_ms",
        "llm_warm_interval_s",
        "prewarm_jobs",
        "deep_pass_thinking",
        "deep_pass_effort",
    ]
    snapshot = {
        "speed": speed,
        "profile_sha256": fingerprint(directory / "profile.json"),
        "settings": settings.model_dump(mode="json", include=set(fields)),
        "code": {
            path.relative_to(root).as_posix(): fingerprint(path)
            for folder in ("apps/gateway/src", "packages/core/src", "packages/protocol/python")
            for path in sorted((root / folder).rglob("*.py"))
        },
    }
    target = directory / "replay-identity.json"
    if target.exists() and json.loads(target.read_text("utf-8")) != snapshot:
        raise ValueError("Replay code/settings changed; use a fresh directory with --reuse-profile")
    save(target, snapshot)


def load_turns(path: Path, year: int) -> list[dict[str, Any]]:
    records = inspect_export(path.read_text("utf-8-sig"), year=year)
    turns = parse_turns(records, confirmed_question_then_suggestion=True)
    return [
        {
            "id": t.record_id,
            "timestamp": t.timestamp.isoformat(),
            "speaker": t.speaker,
            "offset_s": t.offset_s,
            "question": t.question,
            "suggestion": t.suggestion,
        }
        for t in turns
    ]


async def execute(args: argparse.Namespace) -> None:
    directory = private_directory(args.private_dir)
    if args.reuse_profile:
        reuse_profile(directory, args.reuse_profile)
    bind_inputs(directory, args.export, args.year)
    if args.max_cost is not None:
        if args.max_cost <= 0:
            raise ValueError("Maximum cost must be positive")
        save(directory / "budget-limit.json", args.max_cost)
    turns = load_turns(args.export, args.year)
    save(directory / "turns.json", turns)
    if args.phase in ("all", "prepare") and not (directory / "profile.json").exists():
        await prepare(
            directory,
            (directory / "resume.txt").read_text("utf-8"),
            (directory / "jd.txt").read_text("utf-8"),
        )
    if args.phase in ("all", "replay"):
        replay_identity(directory, args.speed)
        target = directory / "replay.json"
        if not target.exists() or not json.loads(target.read_text("utf-8")).get("complete"):
            await replay(directory, turns, speed=args.speed)
    if args.phase in ("all", "judge"):
        from copilot_evals.sensei_diagnosis import diagnose
        from copilot_evals.sensei_judge import judge_run

        await judge_run(directory, turns)
        await diagnose(directory)
    if args.phase in ("all", "report"):
        from copilot_evals.sensei_report import report

        report(directory, args.report, args.fixtures)
        if args.baseline:
            from copilot_evals.sensei_report import compare

            compare(args.baseline, directory, args.report.with_suffix(".comparison.md"))
    print(json.dumps({"phase": args.phase, "records": len(turns), "status": "complete"}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--phase", choices=("all", "prepare", "replay", "judge", "report"), default="all"
    )
    parser.add_argument("--max-cost", type=float, help="Persist an explicit API spend guard in USD")
    parser.add_argument("--reuse-profile", type=Path, help="Reuse a prior private run's fixed pack")
    parser.add_argument("--baseline", type=Path, help="Prior private run to compare scores against")
    parser.add_argument(
        "--report", type=Path, default=Path("docs/analysis/2026-11-19-benchmark-sensei.md")
    )
    parser.add_argument(
        "--fixtures", type=Path, default=Path("evals/sensei/turncare-paraphrased.json")
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="1 preserves event spacing; other speeds are not live latency runs",
    )
    args = parser.parse_args(argv)
    # Provider failures can contain submitted text. No application logs go to console.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(execute(args))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
