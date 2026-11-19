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
    directory: Path,
    summary: dict[str, Any],
    grades: list[dict[str, Any]],
    *,
    ai: bool = False,
) -> dict[str, Any]:
    target = directory / ("ai-review.json" if ai else "human-review.json")
    if not target.exists():
        return {"status": "pending", "completed": 0, "required": 15}
    document = json.loads(target.read_text("utf-8"))
    reviews = document["items"] if ai else document
    if ai and document.get("reviewer_kind") != "ai":
        raise ValueError("AI review provenance missing")
    by_id = {q["id"]: q for q in summary["questions"]}
    errors: dict[str, list[float]] = {criterion: [] for criterion in RUBRIC}
    agree: list[bool] = []
    completed = 0
    seen: set[int] = set()
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
        if review["id"] in seen:
            raise ValueError("Duplicate calibration question")
        seen.add(review["id"])
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
        "status": "ai_agreement_only"
        if ai
        else "passes_predeclared_threshold"
        if valid
        else "unvalidated",
        "reviewer_kind": "ai" if ai else "human",
        "model": document.get("model") if ai else None,
        "meets_agreement_threshold": valid,
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


def eligibility_scope(
    directory: Path, rows: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Exclude only an audited explicit other addressee; retain every unclear case."""
    labelled = [row for row in rows if row["needs_answer"]]
    target = directory / "eligibility.json"
    if not target.exists():
        return None, labelled
    audit = json.loads(target.read_text("utf-8"))
    items = audit["items"]
    expected = {row["id"] for row in labelled}
    identifiers = [item["id"] for item in items]
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != expected:
        raise ValueError("Eligibility audit must cover each response-seeking turn exactly once")
    if any(item["addressee"] not in ("candidate", "other", "unclear") for item in items):
        raise ValueError("Invalid eligibility addressee")
    excluded = {item["id"] for item in items if item["addressee"] == "other"}
    # Only label metadata is eligible for publication; private audit reasoning stays private.
    public = {
        "model": audit["model"],
        "items": [{"id": item["id"], "addressee": item["addressee"]} for item in items],
    }
    return public, [row for row in labelled if row["id"] not in excluded]


def compare(baseline: Path, current: Path, output: Path) -> None:
    """Compare identical evidence and turn sets without publishing source content."""
    from copilot_evals.sensei_benchmark import fingerprint

    for name in ("inputs.json", "profile.json", "annotations.json"):
        if fingerprint(baseline / name) != fingerprint(current / name):
            raise ValueError("Before/after evidence, pack or labels differ")
    eligibility_paths = [path / "eligibility.json" for path in (baseline, current)]
    if any(path.exists() for path in eligibility_paths) and (
        not all(path.exists() for path in eligibility_paths)
        or fingerprint(eligibility_paths[0]) != fingerprint(eligibility_paths[1])
    ):
        raise ValueError("Before/after addressee eligibility differs")
    old_judge, new_judge = [
        json.loads((path / "judge-identity.json").read_text("utf-8"))
        for path in (baseline, current)
    ]
    for key in ("model", "rubric_sha256", "evidence_sha256", "seed"):
        if old_judge[key] != new_judge[key]:
            raise ValueError("Before/after judging protocol differs")
    before, after = [
        json.loads((path / "summary.json").read_text("utf-8")) for path in (baseline, current)
    ]
    if before["judge_model"] != after["judge_model"]:
        raise ValueError("Before/after judges differ")
    _, old_scope = eligibility_scope(baseline, before["questions"])
    _, new_scope = eligibility_scope(current, after["questions"])
    old = {row["id"]: row for row in old_scope}
    new = {row["id"]: row for row in new_scope}
    if old.keys() != new.keys() or not old:
        raise ValueError("Before/after question sets differ or are empty")
    lines = [
        "# Sensei before/after comparison",
        "",
        "Same source evidence, pack, labels, addressee eligibility and judge. "
        "Explicit other addressees are excluded; unclear cases are retained. "
        "Provisional until human calibration.",
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
    original_scope = [r for r in rows if r["needs_answer"]]
    eligibility, actionable = eligibility_scope(directory, rows)
    primary_ids = {row["id"] for row in actionable}
    excluded_ids = sorted(row["id"] for row in original_scope if row["id"] not in primary_ids)
    eligibility_labels = (
        {item["id"]: item["addressee"] for item in eligibility["items"]} if eligibility else {}
    )
    eligibility_counts = Counter(eligibility_labels.values())
    eligibility_note = (
        f"Independent addressee audit: {eligibility['model']}; "
        f"{eligibility_counts['candidate']} candidate, {eligibility_counts['other']} other, "
        f"{eligibility_counts['unclear']} unclear. It used interviewer context only, without "
        "either assistant's answers or grades. Only explicit other addressees are excluded; "
        f"unclear cases remain in the primary scope. Excluded turn IDs: {excluded_ids}. "
        "Eligibility remains model-adjudicated and provisional."
        if eligibility
        else "No independent addressee audit is available; the primary scope retains all "
        "original response-seeking labels, which remain provisional."
    )
    human_cal = calibration(directory, summary, grades)
    cal = (
        calibration(directory, summary, grades, ai=True)
        if (directory / "ai-review.json").exists() and human_cal["completed"] < 15
        else human_cal
    )
    cal["human_completed"] = human_cal["completed"]
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
        "Answer quality is the primary outcome. **Provisional model grades.** "
        + (
            f"At the user's request, {cal['completed']}/15 samples received independent AI "
            "review instead of human scoring. AI agreement does not establish human validity. "
            if cal.get("reviewer_kind") == "ai"
            else f"Human calibration has {cal['completed']}/15 completed reviews. "
        )
        + "No production prompts or answer code changed.",
        "",
        f"{len(rows)} exported suggestions over 45 minutes 13 seconds; {len(original_scope)} "
        "turns originally labelled as requiring a response by a question-only annotator. "
        f"The primary candidate-question scope contains {n} turns. Original labels are "
        "retained below as a sensitivity result; all exported turns still entered the replay.",
        "",
        eligibility_note,
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
        "Sensei's original model, prompts, profile version and conversation context were "
        "not independently reconstructed or verified. This compares historical exported "
        "suggestions with our controlled replay, judged against the supplied resume/JD; "
        "it does not isolate answer engines under identical upstream conditions.",
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
    fast_sensei_mean = mean(
        [statistics.mean(scored[r["id"]]["sensei"]["initial"]) for r in fast_rows]
    )
    lines += [
        f"Conditional initial quality: {fast_mean} "
        f"versus Sensei {fast_sensei_mean} on the same {len(fast_rows)} questions. "
        "Conditional deep quality: "
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
        "### Eligibility sensitivity",
        "",
        "The original denominator remains visible to show the effect of addressee adjudication. "
        "Excluding another participant's question does not change any generated answer or grade.",
        "",
        "| Scope | N | Detections | Initial outputs | Our initial | Our expanded | Sensei "
        "| Initial win / tie / loss / disagreement |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for label, subset in (
        ("Primary candidate scope", actionable),
        ("Original response-seeking labels", original_scope),
    ):
        scope_means = [
            mean([statistics.mean(scored[r["id"]][side][stage]) for r in subset])
            for side, stage in (("ours", "initial"), ("ours", "expanded"), ("sensei", "initial"))
        ]
        scope_counts = Counter(scored[r["id"]]["preference"] for r in subset)
        outcomes = " / ".join(
            str(scope_counts[key]) for key in ("ours", "tie", "sensei", "disagreement")
        )
        scope_detected = sum(r["detected"] and not r["cancelled"] for r in subset)
        scope_outputs = sum(bool(r["fast"]) for r in subset)
        lines.append(
            f"| {label} | {len(subset)} | {scope_detected} | {scope_outputs} "
            f"| {' | '.join(scope_means)} | {outcomes} |"
        )
    lines += [
        "",
        "## Coverage and timing",
        "",
        f"Confirmed/visible detections: {detected}/{n}. "
        f"Initial answers: {len(fast_rows)}/{n}; deep answers: {len(deep_rows)}/{n}.",
        "Coverage uses strict exported-turn card attribution, not semantic-question recall. "
        "Nearby turns can continue or merge one question (potential cases include 30/31 and "
        "35/36), so a missing distinct card does not by itself establish an omitted semantic "
        "question. Answers and grades are never reused across those source turns.",
        f"Cards on turns labelled non-response-seeking: "
        f"{sum(bool(r['fast']) for r in rows if not r['needs_answer'])}/"
        f"{sum(not r['needs_answer'] for r in rows)}. "
        "These are provisional false-positive candidates, not confirmed errors.",
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
        if row["id"] not in causes:
            raise ValueError("A primary ranked loss lacks a diagnosis; regenerate diagnosis")
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
        if row["type"] == "small_talk":
            experiment = (
                "Investigate addressee and cue usefulness before any trigger implementation. "
                + experiment
            )
        proposals.append(
            (rank, f"| {row['id']} | {experiment} | {category} | +{estimate:.3f} / {effort} |")
        )
    diagnosed_top_ids = {
        row["id"]
        for row in losses[:10]
        if statistics.mean(scored[row["id"]]["sensei"]["initial"])
        > statistics.mean(scored[row["id"]]["ours"]["initial"])
    }
    for cause in diagnosis["losses"]:
        if cause["id"] in diagnosed_top_ids or cause["id"] not in primary_ids:
            continue
        q = scored[cause["id"]]
        our_score = statistics.mean(q["ours"]["initial"])
        gap = statistics.mean(q["sensei"]["initial"]) - our_score
        recoverable = gap if gap > 0 else 5 - our_score
        basis = "relative deficit" if gap > 0 else "absolute quality; headroom to 5"
        estimate = 0.25 * recoverable / max(n, 1)
        effort = cause["effort"]
        rank = estimate / {"small": 1, "medium": 3, "large": 8}[effort]
        experiment = cause["experiment"].replace("|", "/").replace("\n", " ")
        proposals.append(
            (
                rank,
                f"| {cause['id']} | {experiment} | {cause['category']} "
                f"| +{estimate:.3f} / {effort} ({basis}) |",
            )
        )
    lines += [
        "",
        "## Answer-quality deficits where an answer appeared",
        "",
        "This separates weak answers from missed detection. Rows are ranked on the "
        "initial score gap on matched questions; zero or negative gaps are omitted.",
        "",
        "| Turn | Paraphrased intent | Initial deficit | Expanded deficit | Judge explanation |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    delivered_deficits = [
        r
        for r in losses
        if r["fast"]
        and statistics.mean(scored[r["id"]]["sensei"]["initial"])
        > statistics.mean(scored[r["id"]]["ours"]["initial"])
    ][:10]
    if not delivered_deficits:
        lines += [
            "",
            "No delivered answer has a positive aggregate initial-score deficit versus Sensei "
            "in this run. This does not imply that every delivered answer is satisfactory.",
        ]
    for row in delivered_deficits:
        q = scored[row["id"]]
        gap = statistics.mean(q["sensei"]["initial"]) - statistics.mean(q["ours"]["initial"])
        if gap <= 0:
            continue
        expanded_gap = statistics.mean(q["sensei"]["expanded"]) - statistics.mean(
            q["ours"]["expanded"]
        )
        grade = next(g for g in grades if g["id"] == row["id"] and g["round"] == 0)
        reason = grade["grade"]["reason"].replace("|", "/").replace("\n", " ")
        intent = row["paraphrase"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {row['id']} | {intent} | {gap:.2f} | {expanded_gap:.2f} | {reason} |")
    absolute_risks = sorted(
        [
            row
            for row in fast_rows
            if statistics.mean(scored[row["id"]]["ours"]["initial"]) < 4
            or any(scored[row["id"]]["ours"]["invented_initial"])
            or any(scored[row["id"]]["ours"]["invented_expanded"])
        ],
        key=lambda row: statistics.mean(scored[row["id"]]["ours"]["initial"]),
    )
    lines += [
        "",
        "### Absolute quality risks in delivered answers",
        "",
        "Relative wins can still have weak scores or unsupported personal claims. "
        "These rows include delivered answers below 4/5 overall or with a nonzero claim "
        "finding in either order. They are ranked by our initial score, not by a Sensei loss. "
        "Claim counts show the two judging orders separately.",
        "",
        "| Turn | Paraphrased intent | Our initial / expanded | Initial claims | Expanded claims |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for row in absolute_risks:
        q = scored[row["id"]]["ours"]
        intent = row["paraphrase"].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {row['id']} | {intent} | {statistics.mean(q['initial']):.2f} / "
            f"{statistics.mean(q['expanded']):.2f} | {q['invented_initial']} | "
            f"{q['invented_expanded']} |"
        )
    if not absolute_risks:
        lines += ["", "No delivered answer met this absolute-risk rule."]
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
        "The user waived human scoring. The separate AI review and addressee audit support "
        "consistency checks, while priorities remain provisional without human calibration. "
        "Opening pleasantries require an addressee/usefulness check before adding triggers. "
        "The ranked "
        "experiments below use an explicit planning assumption: recovering 25% of each "
        "observed deficit. The estimated gain is on the overall 1-5 initial score, divided "
        "by relative effort (small=1, medium=3, large=8) for ordering. These estimates are "
        "not measured effects, overlap across experiments, and must not be summed. "
        "For absolute-quality follow-ups that are not relative losses, the same 25% assumption "
        "applies to headroom between our initial score and 5; those entries are explicitly "
        "labelled and do not claim a measured Sensei deficit.",
        "",
        "| Turn | Proposed experiment | Loss reason | Assumed score gain / effort |",
        "| --- | --- | --- | --- |",
        *[proposal for _, proposal in sorted(proposals, key=lambda item: item[0], reverse=True)],
        "",
        "Every proposed change must compare per-criterion and per-type scores, agreement, "
        "unsupported claims, coverage and win regressions on the same evidence. No changes "
        "from this plan have been made.",
        "",
        "## Calibration and reproducibility",
        "",
        "The private human-review.html/JSON contains 15 seeded random questions from the "
        "original actionable-turn labels, with A/B answers and score fields. "
        + (
            f"The user requested AI scoring instead. {cal.get('model')} independently graded "
            "the same initial-answer sample once, with no primary grades or product labels. "
            "The table measures AI-to-AI agreement, not human calibration. Both reviewers "
            "share a provider family, so correlated biases remain possible. "
            if cal.get("reviewer_kind") == "ai"
            else "The agreement results below reflect saved human scores. "
        )
        + "The original human trust threshold was 15 complete, preference agreement >=80%, "
        "and each criterion's mean absolute error <=0.75. Meeting that numerical threshold "
        "with AI scores does not satisfy the human trust gate.",
        "",
        "Run the command in evals/README.md with the external export and private artifacts. "
        "Resume/pack construction is cached for comparable before/after runs. The committed "
        "fixture contains paraphrased questions only; original wording and Sensei answers are "
        "required locally to reproduce the original benchmark. A paraphrase-only regression "
        "run is a distinct experiment, not an exact reproduction.",
        "",
        "Calibration status: **" + cal["status"] + "**. "
        f"Completed {cal['completed']}/15. Preference agreement: "
        + (
            f"{cal['preference_agreement']:.1%}"
            if cal.get("preference_agreement") is not None
            else "unavailable"
        )
        + ".",
        "",
        "| Calibration criterion | Mean absolute error |",
        "| --- | ---: |",
        *[
            f"| {key} | {value:.3f} |" if value is not None else f"| {key} | unavailable |"
            for key, value in cal.get("criterion_mae", {}).items()
        ],
        "",
        "Review covers initial answers only; expanded grades remain model-only. "
        f"Human reviews completed: {cal['human_completed']}/15.",
        "",
        "## Spend",
        "",
        f"Usage-accounted calls: ${known_cost:.6f}. "
        f"Conservative unresolved-call/embedding reservations: "
        f"${unresolved_cost:.6f}. "
        f"**Combined conservative ledger: ${known_cost + unresolved_cost:.6f}.** "
        "The user lifted the original $3 cap. The ledger includes the earlier aborted replay "
        "and failed preparation attempts. Unknown usage is reserved conservatively. "
        "No new billed infrastructure. Usage-accounted amounts apply configured token prices; "
        "cache-write premiums and billing adjustments are not exposed by the live adapter. "
        "The combined ledger is an estimate with unresolved allowances, not an invoice "
        "or a guaranteed billing upper bound.",
        "",
        "Prices checked against [OpenAI pricing](https://developers.openai.com/api/docs/pricing) "
        "and [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). "
        "Setup, embeddings, detector calls, prewarming, fast/deep generation, annotations and "
        "both judge orders, independent AI review and the addressee audit are included. "
        "Independent reviewer rates use the [GPT-6.2 Astra model page]"
        "(https://developers.openai.com/api/docs/models/gpt-6.2-astra).",
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
        "| Turn | Expected / detected type | Our initial / expanded | Sensei | Preference "
        "| Claims ours / Sensei | Fast first / deep first (s) | Large score disagreement |",
        "| --- | --- | ---: | ---: | --- | --- | --- | --- |",
    ]
    for row in actionable:
        q = scored[row["id"]]
        initial = statistics.mean(q["ours"]["initial"])
        expanded = statistics.mean(q["ours"]["expanded"])
        competitor = statistics.mean(q["sensei"]["initial"])
        firsts = [
            f"{row['latency'][key]:.2f}" if key in row["latency"] else "missing"
            for key in ("fast_first_s", "deep_first_s")
        ]
        lines.append(
            f"| {row['id']} | {row['type']} / {row.get('qtype') or 'not detected'} "
            f"| {initial:.2f} / {expanded:.2f} "
            f"| {competitor:.2f} | {q['preference']} "
            f"| {q['ours']['invented_initial']} / {q['sensei']['invented_initial']} "
            f"| {firsts[0]} / {firsts[1]} "
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
        "eligibility": eligibility,
        "primary_question_ids": sorted(primary_ids),
        "turns": [
            {
                "id": r["id"],
                "offset_s": r["offset_s"],
                "question": r["paraphrase"],
                "type": r["type"],
                "needs_answer": r["needs_answer"],
                "addressee": eligibility_labels.get(r["id"], "not_audited"),
                "in_primary_scope": r["id"] in primary_ids,
            }
            for r in rows
        ],
    }
    overlap_check(json.dumps(fixture), rows)
    fixtures.parent.mkdir(parents=True, exist_ok=True)
    save(fixtures, fixture)
    # Scores contain IDs and numbers only; preserve an auditable public baseline.
    save(
        fixtures.with_suffix(".scores.json"),
        {
            **summary,
            "calibration": cal,
            "eligibility": eligibility,
            "primary_question_ids": sorted(primary_ids),
            "excluded_question_ids": excluded_ids,
        },
    )
