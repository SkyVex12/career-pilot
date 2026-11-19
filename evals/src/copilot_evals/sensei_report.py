"""Aggregate private measurements into a paraphrased, reviewable benchmark report."""

import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from copilot_evals.sensei_judge import RUBRIC, SEED
from copilot_evals.sensei_runtime import save


def calibration(
    directory: Path, summary: dict[str, Any], grades: list[dict[str, Any]]
) -> dict[str, Any]:
    target = directory / "human-review.json"
    if not target.exists():
        return {"status": "pending", "completed": 0, "required": 15}
    reviews = json.loads(target.read_text("utf-8"))
    by_id = {q["id"]: q for q in summary["questions"]}
    errors: dict[str, list[float]] = {criterion: [] for criterion in RUBRIC}
    agree: list[bool] = []
    completed = 0
    for review in reviews:
        if review["preference"] not in ("A", "B", "tie"):
            continue
        if any(
            type(review[side]["scores"].get(c)) is not int
            or not 1 <= review[side]["scores"][c] <= 5
            for side in ("A", "B")
            for c in RUBRIC
        ):
            continue
        item = by_id[review["id"]]
        first = next(g for g in grades if g["id"] == review["id"] and g["round"] == 0)
        human = (
            first["order"]["AB".index(review["preference"])]
            if review["preference"] != "tie"
            else "tie"
        )
        agree.append(human == item["preference"])
        for label, side in zip(("A", "B"), first["order"], strict=True):
            for i, criterion in enumerate(RUBRIC):
                errors[criterion].append(
                    abs(review[label]["scores"][criterion] - item[side]["initial"][i])
                )
        completed += 1
    agreement = statistics.mean(agree) if agree else None
    maes = {k: statistics.mean(v) if v else None for k, v in errors.items()}
    valid = (
        completed == 15
        and agreement is not None
        and agreement >= 0.8
        and all(value is not None and value <= 0.75 for value in maes.values())
    )
    return {
        "status": "passes_predeclared_threshold" if valid else "unvalidated",
        "completed": completed,
        "required": 15,
        "preference_agreement": agreement,
        "criterion_mae": maes,
        "threshold": "15 complete; preference agreement >=80%; each criterion MAE <=0.75",
    }


def mean(values: list[float]) -> str:
    return f"{statistics.mean(values):.2f}" if values else "N/A"


def percentile(values: list[float], percent: float) -> str:
    if not values:
        return "N/A"
    ordered = sorted(values)
    return f"{ordered[max(0, math.ceil(len(values) * percent) - 1)]:.2f}"


def overlap_check(text: str, turns: list[dict[str, Any]], width: int = 9) -> None:
    """Fail closed on long verbatim spans before publishing any model paraphrases."""

    def words(value: str) -> list[str]:
        return re.findall(r"\w+", value.casefold())

    original: set[tuple[str, ...]] = set()
    for turn in turns:
        for field in ("question", "suggestion"):
            tokens = words(turn[field])
            original.update(tuple(tokens[i : i + width]) for i in range(len(tokens) - width + 1))
    tokens = words(text)
    if any(tuple(tokens[i : i + width]) in original for i in range(len(tokens) - width + 1)):
        raise ValueError("Publication contains a verbatim source span; rewrite before committing")


def compare(baseline: Path, current: Path, output: Path) -> None:
    """Compare identical evidence and turn sets without publishing source content."""
    from copilot_evals.sensei_benchmark import fingerprint

    for name in ("inputs.json", "profile.json", "annotations.json"):
        if fingerprint(baseline / name) != fingerprint(current / name):
            raise ValueError("Before/after evidence, pack or labels differ")
    before, after = [
        json.loads((path / "summary.json").read_text("utf-8")) for path in (baseline, current)
    ]
    if before["judge_model"] != after["judge_model"]:
        raise ValueError("Before/after judges differ")
    old = {row["id"]: row for row in before["questions"]}
    new = {row["id"]: row for row in after["questions"]}
    if old.keys() != new.keys() or not old:
        raise ValueError("Before/after question sets differ or are empty")
    lines = [
        "# Sensei before/after comparison",
        "",
        "Same source evidence, pack, labels and judge. Provisional until human calibration.",
        "",
        "| Criterion | Initial before | Initial after | Change | Expanded change |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for i, criterion in enumerate(RUBRIC):
        left = statistics.mean(row["ours"]["initial"][i] for row in old.values())
        right = statistics.mean(row["ours"]["initial"][i] for row in new.values())
        expanded = statistics.mean(
            new[key]["ours"]["expanded"][i] - old[key]["ours"]["expanded"][i] for key in old
        )
        lines.append(
            f"| {criterion} | {left:.2f} | {right:.2f} | {right - left:+.2f} | {expanded:+.2f} |"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report(directory: Path, output: Path, fixtures: Path) -> None:
    summary = json.loads((directory / "summary.json").read_text("utf-8"))
    rows = json.loads((directory / "answers.json").read_text("utf-8"))
    grades = json.loads((directory / "grades.json").read_text("utf-8"))
    costs = json.loads((directory / "cost.json").read_text("utf-8"))
    run = json.loads((directory / "replay.json").read_text("utf-8"))
    diagnosis = json.loads((directory / "diagnosis.json").read_text("utf-8"))
    causes = {item["id"]: item for item in diagnosis["losses"]}
    strengths = {item["id"]: item["reason"] for item in diagnosis["strengths"]}
    scored = {q["id"]: q for q in summary["questions"]}
    actionable = [r for r in rows if r["needs_answer"]]
    cal = calibration(directory, summary, grades)
    save(directory / "calibration.json", cal)
    n = len(actionable)
    known_cost = sum(c["charged_usd"] for c in costs if c["status"] == "usage")
    unresolved_cost = sum(c["charged_usd"] for c in costs if c["status"] != "usage")
    detected = sum(r["detected"] and not r["cancelled"] for r in actionable)
    lines = [
        "# Sensei benchmark — 2026-11-19",
        "",
        "## Status and scope",
        "",
        "Answer quality is the primary outcome. **Provisional model grades: human calibration "
        f"has {cal['completed']}/15 completed reviews.** "
        "No production prompts or answer code changed.",
        "",
        f"{len(rows)} exported suggestions over 45 minutes 13 seconds; {n} turns labelled as "
        "requiring a response by a question-only annotator. The remaining turns are reported "
        "separately from the quality denominator. These labels require human review too.",
        "",
        "Sergio was not present in the copilot. The supplied three-page resume and TurnCare "
        "Full Stack Developer JD were used to create a fresh, isolated benchmark pairing. "
        "The actual worker extraction, node validation, digest renderer and interview pack "
        "builder were used with in-memory storage. Preparation used gpt-6.1-sol instead of "
        "the locally configured, unpriced gpt-6-sol extraction route, and a longer provider "
        "timeout after a pack timeout. This is not a historical reconstruction "
        "of an existing pack.",
        "",
        "Replay used the actual gateway with its configured detector, gpt-6-luna fast model, "
        "gpt-6.1-sol deep model and light-deep routing, prepared-answer embeddings, cancellation "
        "and conversation memory. Local storage and text STT replaced infrastructure and audio. "
        "Only interviewer turns entered the gateway; it never received Sensei suggestions or "
        "candidate speech. Consequently memory builds from interviewer turns and app answers, "
        "not from the candidate's missing spoken responses.",
        "",
        f"Replay speed: {run['speed']}x. Export timestamp semantics are unknown: "
        "timing is measured "
        "from text injection, **not verified question end**. STT, speech duration and overlay "
        "render time are excluded. Sensei generation latency is unavailable. The exported sample "
        "contains only turns shown to Sensei, so its observed coverage is 100% by construction; "
        "this does not establish its recall over the full interview.",
        "",
        "## Scoring",
        "",
        f"Independent judge: {summary['judge_model']}; six equally weighted 1-5 criteria. "
        f"Seed {SEED}; each turn judged twice in reversed A/B order. The judge sees source "
        "resume/JD evidence and anonymized answers, not product names. Citation markers are "
        "removed symmetrically. The configured Anthropic credential failed a metadata check, "
        "so GPT-6 Astra was used. It differs from both generators but shares their provider "
        "family; writing style can still reveal provenance. Source text is data, "
        "not instructions. No new Sensei answers were generated.",
        "",
        "Initial = our fast answer versus the exported Sensei suggestion. Expanded = our deep "
        "answer when produced, otherwise fast; Sensei uses the same exported suggestion. "
        "Empty answers score 1 in the coverage-inclusive tables below. Conditional scores "
        "exclude empty answers. No best-of-pass cherry-picking.",
        "",
        "| Criterion | Our initial | Our expanded/fast fallback | Sensei |",
        "| --- | ---: | ---: | ---: |",
    ]
    for i, criterion in enumerate(RUBRIC):
        values = [
            mean([scored[r["id"]][side][stage][i] for r in actionable])
            for side, stage in (("ours", "initial"), ("ours", "expanded"), ("sensei", "initial"))
        ]
        lines.append(f"| {criterion} | {' | '.join(values)} |")
    values = [
        mean([statistics.mean(scored[r["id"]][side][stage]) for r in actionable])
        for side, stage in (("ours", "initial"), ("ours", "expanded"), ("sensei", "initial"))
    ]
    lines += [f"| **Overall** | {' | '.join(values)} |", ""]
    fast_rows = [r for r in actionable if r["fast"]]
    deep_rows = [r for r in actionable if r["deep"]]
    fast_mean = mean([statistics.mean(scored[r["id"]]["ours"]["initial"]) for r in fast_rows])
    lines += [
        f"Conditional initial quality: {fast_mean} "
        f"(n={len(fast_rows)}). Conditional deep quality: "
        f"{mean([statistics.mean(scored[r['id']]['ours']['expanded']) for r in deep_rows])} "
        f"(n={len(deep_rows)}).",
        "",
        "| Question type | N | Our initial | Our expanded | Sensei |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for kind in sorted({r["type"] for r in actionable}):
        subset = [r for r in actionable if r["type"] == kind]
        values = [
            mean([statistics.mean(scored[r["id"]][side][stage]) for r in subset])
            for side, stage in (("ours", "initial"), ("ours", "expanded"), ("sensei", "initial"))
        ]
        lines.append(f"| {kind} | {len(subset)} | {' | '.join(values)} |")
    lines += [
        "",
        "| Pairwise result | Win | Tie | Loss | Order disagreement |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for stage in ("preference", "expanded_preference"):
        counts = Counter(scored[r["id"]][stage] for r in actionable)
        cells = [
            f"{counts[key]} ({counts[key] / max(1, n):.1%})"
            for key in ("ours", "tie", "sensei", "disagreement")
        ]
        lines.append(f"| {stage} | {' | '.join(cells)} |")
    lines += [
        "",
        "Order disagreements remain unresolved; they are not counted as ties.",
        "",
        "## Coverage and timing",
        "",
        f"Confirmed/visible detections: {detected}/{n}. "
        f"Initial answers: {len(fast_rows)}/{n}; deep answers: {len(deep_rows)}/{n}.",
        "",
        "| Stage, seconds from injection | p50 | p95 | N |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key in ("detected_s", "fast_first_s", "deep_first_s", "fast_done_s", "deep_done_s"):
        values = [r["latency"][key] for r in actionable if key in r["latency"]]
        lines.append(
            f"| {key} | {percentile(values, 0.5)} | {percentile(values, 0.95)} | {len(values)} |"
        )
    late = [r for r in actionable if r["latency"].get("fast_first_s", 0) > 2]
    after_next = sum(
        "fast_first_s" in row["latency"]
        and row["latency"]["fast_first_s"]
        > (actionable[i + 1]["offset_s"] - row["offset_s"]) / run["speed"]
        for i, row in enumerate(actionable[:-1])
    )
    lines += [
        "",
        f"Initial answers after a predeclared 2-second injection deadline: {len(late)}. "
        "Missed answers are counted separately, never assigned a latency of zero.",
        f"Answers first appearing after the next response-seeking turn: {after_next}. "
        "This uses recorded spacing, without assuming that timestamps mark question ends.",
        "",
        "## Ten largest initial-answer deficits",
        "",
        "Ranked by equal-weight score gap, not selected anecdotes. Categories below are "
        "diagnostic hypotheses unless directly established by events. Retrieval versus "
        "missing memory requires checking the private profile/pack evidence; scores alone "
        "cannot distinguish those causes.",
        "",
        "| Turn | Paraphrased intent | Score deficit | Classification | Judge explanation |",
        "| --- | --- | ---: | --- | --- |",
    ]
    losses = sorted(
        actionable,
        key=lambda r: (
            statistics.mean(scored[r["id"]]["sensei"]["initial"])
            - statistics.mean(scored[r["id"]]["ours"]["initial"])
        ),
        reverse=True,
    )
    categories: Counter[str] = Counter()
    proposals: list[tuple[float, str]] = []
    for row in losses[:10]:
        q = scored[row["id"]]
        gap = statistics.mean(q["sensei"]["initial"]) - statistics.mean(q["ours"]["initial"])
        if gap <= 0:
            continue
        cause = causes[row["id"]]
        category = cause["category"]
        categories[category] += 1
        reason = str(cause["reason"]).replace("|", "/").replace("\n", " ")
        intent = row["paraphrase"].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {row['id']} | {intent} | {gap:.2f} | {category} "
            f"({cause['evidence_level']}) | {reason} |"
        )
        effort = cause["effort"]
        estimate = 0.25 * gap / max(n, 1)
        rank = estimate / {"small": 1, "medium": 3, "large": 8}[effort]
        experiment = cause["experiment"].replace("|", "/").replace("\n", " ")
        proposals.append(
            (rank, f"| {row['id']} | {experiment} | {category} | +{estimate:.3f} / {effort} |")
        )
    lines += ["", "## Strengths to preserve", ""]
    wins = [r for r in actionable if scored[r["id"]]["preference"] == "ours"]
    for row in wins[:10]:
        lines.append(f"- Turn {row['id']}: {row['paraphrase']} — {strengths.get(row['id'], '')}")
    if not wins:
        lines.append("No initial-answer win agreed across both orders in this run.")
    lines += [
        "",
        "## Improvement plan — approval required before implementation",
        "",
        "The priorities are provisional until calibration. Expected gain is a testable "
        "hypothesis, not a measured improvement. Effort estimates are relative.",
        "",
        "Validate the 15 human samples and actionable-turn labels first. The ranked "
        "experiments below use an explicit planning assumption: recovering 25% of each "
        "observed deficit. The estimated gain is on the overall 1-5 initial score, divided "
        "by relative effort (small=1, medium=3, large=8) for ordering. These estimates are "
        "not measured effects, overlap across experiments, and must not be summed.",
        "",
        "| Loss turn | Proposed experiment | Loss reason | Assumed score gain / effort |",
        "| --- | --- | --- | --- |",
        *[proposal for _, proposal in sorted(proposals, key=lambda item: item[0], reverse=True)],
        "",
        "Every proposed change must compare per-criterion and per-type scores, agreement, "
        "unsupported claims, coverage and win regressions on the same evidence. No changes "
        "from this plan have been made.",
        "",
        "## Calibration and reproducibility",
        "",
        "The private human-review.html/JSON contains 15 seeded random actionable questions, "
        "A/B answers and blank scores. Judge agreement is unavailable until the user scores "
        "them. Predeclared trust threshold: 15 complete, preference agreement >=80%, and "
        "each criterion's mean absolute error <=0.75. A failed threshold requires adjudication "
        "or rubric revision, not acceptance of these grades.",
        "",
        "Run the command in evals/README.md with the external export and private artifacts. "
        "Resume/pack construction is cached for comparable before/after runs. The committed "
        "fixture contains paraphrased questions only; original wording and Sensei answers are "
        "required locally to reproduce the original benchmark. A paraphrase-only regression "
        "run is a distinct experiment, not an exact reproduction.",
        "",
        "## Spend",
        "",
        f"Usage-accounted calls: ${known_cost:.6f}. "
        f"Conservative unresolved-call/embedding reservations: "
        f"${unresolved_cost:.6f}. "
        f"**Combined conservative ledger: ${known_cost + unresolved_cost:.6f}.** "
        "The user lifted the original $3 cap. The ledger includes the earlier aborted replay "
        "and failed preparation attempts. Unknown usage is reserved conservatively. "
        "No new billed infrastructure. This is API usage plus allowances, not an invoice.",
        "",
        "Prices checked against [OpenAI pricing](https://developers.openai.com/api/docs/pricing) "
        "and [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). "
        "Setup, embeddings, detector calls, prewarming, fast/deep generation, annotations and "
        "both judge orders are included.",
        "",
    ]
    cost_rows: list[str] = []
    for model in sorted({c["model"] for c in costs}):
        subset = [c for c in costs if c["model"] == model]
        used = sum(c["charged_usd"] for c in subset if c["status"] == "usage")
        reserved = sum(c["charged_usd"] for c in subset if c["status"] != "usage")
        cost_rows.append(f"| {model} | {len(subset)} | {used:.6f} | {reserved:.6f} |")
    lines += [
        "| Model | Calls | Usage-accounted USD | Unresolved allowance USD |",
        "| --- | ---: | ---: | ---: |",
        *cost_rows,
        "",
        "## Unsupported personal claims",
        "",
        "Counts are judge findings against the supplied resume/JD, not independently verified "
        "fabrications. A claim absent from these sources may still be true. The two order "
        "counts are shown separately so disagreement is visible; they are not added together.",
        "",
        "| Side and answer | Order 1 total | Order 2 total |",
        "| --- | ---: | ---: |",
    ]
    for side in ("ours", "sensei"):
        for stage in ("initial", "expanded"):
            counts = [
                sum(scored[r["id"]][side][f"invented_{stage}"][i] for r in actionable)
                for i in range(2)
            ]
            lines.append(f"| {side} {stage} | {counts[0]} | {counts[1]} |")
    lines += [
        "",
        "## Per-question audit",
        "",
        "Scores average six criteria across both orders. Claim counts are initial-order "
        "pairs. Stage timings, answer text and source references remain in private answers.json.",
        "",
        "| Turn | Type | Our initial / expanded | Sensei | Preference | Claims ours / Sensei "
        "| Large score disagreement |",
        "| --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for row in actionable:
        q = scored[row["id"]]
        initial = statistics.mean(q["ours"]["initial"])
        expanded = statistics.mean(q["ours"]["expanded"])
        competitor = statistics.mean(q["sensei"]["initial"])
        lines.append(
            f"| {row['id']} | {row['type']} | {initial:.2f} / {expanded:.2f} "
            f"| {competitor:.2f} | {q['preference']} "
            f"| {q['ours']['invented_initial']} / {q['sensei']['invented_initial']} "
            f"| {q['score_disagreement']} |"
        )
    text = "\n".join(lines) + "\n"
    overlap_check(text, rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    fixture = {
        "source": "Paraphrased Sensei export; raw export required for exact comparison",
        "rubric": RUBRIC,
        "judge": summary["judge_model"],
        "seed": SEED,
        "turns": [
            {
                "id": r["id"],
                "offset_s": r["offset_s"],
                "question": r["paraphrase"],
                "type": r["type"],
                "needs_answer": r["needs_answer"],
            }
            for r in rows
        ],
    }
    overlap_check(json.dumps(fixture), rows)
    fixtures.parent.mkdir(parents=True, exist_ok=True)
    save(fixtures, fixture)
    # Scores contain IDs and numbers only; preserve an auditable public baseline.
    save(fixtures.with_suffix(".scores.json"), {**summary, "calibration": cal})
