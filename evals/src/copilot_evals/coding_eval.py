"""Coding mode: answers to the tasks on a screen, from screenshots, checked by running them.

    uv run copilot-coding-eval render                     # pages/*.html -> shots/*.png (Edge)
    uv run copilot-coding-eval run --label base           # both answer modes, every case
    uv run copilot-coding-eval run --label x --only mixed --modes assessment
    uv run copilot-coding-eval compare base x             # markdown table

The cases (evals/coding/cases.toml) are original problem pages in the layout of the usual
assessment platforms, rendered to 1568 x 882 PNGs by headless Microsoft Edge: a Python and a
JavaScript problem with starter code, single and multiple choice, an output question, a bug to
fix, SQL, a short answer, a page mixing two choices and code, a long problem in 2 shots, a
problem whose helper and solution files are 2 more shots, a system design prompt, and a desktop
with no task.

Each run goes through the gateway's own prompt builders (`copilot_core.screen`) and model
routes (`.env`): the fast pass streams, its READ header is taken out as the gateway does, and the
code pass starts the moment the header shows a task that needs code (or when the fast pass ends
without one), so the timings are the gateway's, less the upload.

Checks, with no model grading:
- kinds: the kinds read, in screen order, equal the case's;
- choice: the options picked equal the key; output: the exact output, in a code block;
- code: the code block for the task is in the right language, defines the function, and passes
  the case's tests (Python and Node, run here, 10 s each; `large` adds 200,000 inputs in 2 s);
- sql: the query returns the expected rows on the case's tables (SQLite);
- short and design: the answer has a word from each keyword group; design has no code;
- no task: nothing read, and no code pass.
Timings from the request: the reading (`read_ms`), the first word shown (`first_word_ms`), the
fast pass done, the code's first token and the code done. Results go to
evals/coding/results/<label>/: runs.json, summary.json and samples.md.
"""

import argparse
import asyncio
import base64
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from copilot_evals.answer_eval import call_cost, format_cost, summarize_costs

ROOT = Path(__file__).resolve().parents[2] / "coding"
PAGES = ROOT / "pages"
SHOTS = ROOT / "shots"
RESULTS = ROOT / "results"
WIDTH, HEIGHT = 1568, 882
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
RUN_TIMEOUT_S = 10.0
LARGE_N = 200_000
LARGE_BUDGET_S = 2.0


# Cases.


@dataclass(frozen=True)
class ShotSpec:
    page: str
    title: str
    offset: int = 0

    @property
    def file(self) -> Path:
        return SHOTS / (f"{self.page}.png" if not self.offset else f"{self.page}_{self.offset}.png")


@dataclass(frozen=True)
class Case:
    id: str
    shots: tuple[ShotSpec, ...]
    kinds: tuple[str, ...]
    tasks: tuple[Mapping[str, Any], ...]
    modes: tuple[str, ...]
    transcript: str


def cases(path: Path = ROOT / "cases.toml") -> list[Case]:
    data = tomllib.loads(path.read_text("utf-8"))
    default = str(data.get("default_transcript", ""))
    out: list[Case] = []
    for c in data["case"]:
        out.append(
            Case(
                id=c["id"],
                shots=tuple(
                    ShotSpec(s["page"], s["title"], int(s.get("offset", 0))) for s in c["shots"]
                ),
                kinds=tuple(c["kinds"]),
                tasks=tuple(c.get("task", [])),
                modes=tuple(c.get("modes", ["interview", "assessment"])),
                transcript=str(c.get("transcript", default)),
            )
        )
    return out


# Rendering.


def edge() -> str:
    for p in EDGE_PATHS:
        if Path(p).exists():
            return p
    found = shutil.which("msedge") or shutil.which("microsoft-edge")
    if not found:
        sys.exit("Microsoft Edge not found: it renders the pages")
    return found


def render(specs: list[ShotSpec]) -> None:
    """Each page at 1568 x 882, scrolled down `offset` pixels, as a PNG in shots/."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    browser = edge()
    for spec in sorted(set(specs), key=lambda s: (s.page, s.offset)):
        page = PAGES / f"{spec.page}.html"
        source = page
        if spec.offset:
            # A copy next to the page (so style.css resolves) with the body pulled up.
            html = page.read_text("utf-8").replace(
                "</head>", f"<style>body{{margin-top:-{spec.offset}px}}</style></head>", 1
            )
            source = PAGES / f"_{spec.page}_{spec.offset}.html"
            source.write_text(html, "utf-8")
        try:
            subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
                 f"--screenshot={spec.file}", source.resolve().as_uri()],
                check=True, capture_output=True, timeout=60,
            )  # fmt: skip
        finally:
            if source != page:
                source.unlink(missing_ok=True)
        print(f"{spec.file.name}: {spec.file.stat().st_size // 1024} KB")


def png_size(data: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", data[16:24])
    return width, height


# Grading helpers.

_FENCE = re.compile(r"```([\w+#.-]*)[^\n]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^#{2,4}\s*(\d+)[.)]", re.MULTILINE)
_LANGS = {
    "python": ("python", "py", "python3"),
    "javascript": ("javascript", "js", "node", "jsx"),
    "sql": ("sql", "postgresql", "postgres", "sqlite"),
}


def sections(text: str) -> dict[int, str]:
    """An answer split by its "### n." headings; all of it as task 1 without headings."""
    marks = list(_HEADING.finditer(text))
    if not marks:
        return {1: text}
    out: dict[int, str] = {}
    if (before := text[: marks[0].start()].strip()) and int(marks[0].group(1)) != 1:
        out[1] = before  # task 1 without its heading, the others with theirs
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = out.get(int(m.group(1)), "") + text[m.start() : end]
    return out


def code_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).lower(), m.group(2)) for m in _FENCE.finditer(text)]


def chosen(text: str) -> list[str]:
    """The option letters on the "Answer:" line."""
    line = next((ln for ln in text.splitlines() if "answer" in ln.lower()), "")
    line = re.sub(r"(?i).*?answers?\**\s*:\**", "", line, count=1)
    return sorted(set(re.findall(r"(?<![A-Za-z])([A-E])(?=[).:,\s]|$)", line)))


def pick_code(text: str, function: str) -> tuple[str, str] | None:
    """The last code block that defines the function (by name)."""
    found = [
        (lang, code) for lang, code in code_blocks(text) if re.search(rf"\b{function}\b", code)
    ]
    return found[-1] if found else None


def language_ok(tag: str, code: str, language: str) -> bool:
    if tag:
        return tag in _LANGS.get(language, (language,))
    if language == "python":
        return "def " in code
    if language == "javascript":
        return "function" in code or "=>" in code
    return False


PY_RUNNER = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
sys.setrecursionlimit(10000)
import solution
name, cases, large = sys.argv[2], json.loads(sys.argv[3]), sys.argv[4] == "1"
fn = getattr(solution, name, None)
if fn is None and hasattr(solution, "Solution"):
    fn = getattr(solution.Solution(), name)
out = []
for args, want in cases:
    try:
        got = fn(*args)
        out.append([got == want, repr(got)])
    except Exception as e:
        out.append([False, f"{type(e).__name__}: {e}"])
if large:
    import random
    random.seed(7)
    data = [random.randrange(0, 10**9) for _ in range(200000)]
    t = time.perf_counter()
    fn(data, 1000)
    out.append(["large", time.perf_counter() - t])
print(json.dumps(out))
"""

JS_RUNNER = """
const name = process.argv[2], cases = JSON.parse(process.argv[3]);
const fn = typeof globalThis[name] === "function" ? globalThis[name]
  : (typeof Solution === "function" ? (...a) => new Solution()[name](...a) : null);
const out = cases.map(([args, want]) => {
  try {
    const got = fn(...args);
    return [JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got)];
  } catch (e) { return [false, String(e)]; }
});
console.log(JSON.stringify(out));
"""

PY_PRELUDE = "from typing import *\nimport collections, heapq, bisect, itertools, math\n"


def run_code(language: str, code: str, task: Mapping[str, Any]) -> dict[str, Any]:
    """Runs the code on the task's tests: how many pass, and the first failure."""
    tests = [[json.loads(a), json.loads(w)] for a, w in task["tests"]]
    name = str(task["function"])
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        try:
            if language == "python":
                (d / "solution.py").write_text(PY_PRELUDE + code, "utf-8")
                if task.get("helper") == "text_utils":
                    (d / "text_utils.py").write_text(TEXT_UTILS, "utf-8")
                (d / "runner.py").write_text(PY_RUNNER, "utf-8")
                cmd = [sys.executable, "-I", str(d / "runner.py"), str(d), name,
                       json.dumps(tests), "1" if task.get("large") else "0"]  # fmt: skip
            else:
                # Function declarations become globals in a script run with `node -e`.
                export = (
                    f"\nglobalThis.{name} = typeof {name} !== 'undefined' ? {name} : undefined;\n"
                )
                script = code + export + JS_RUNNER
                (d / "run.js").write_text(script, "utf-8")
                cmd = ["node", str(d / "run.js"), name, json.dumps(tests)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, cwd=d)
        except subprocess.TimeoutExpired:
            return {"passed": 0, "total": len(tests), "error": "timeout"}
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"passed": 0, "total": len(tests), "error": proc.stderr.strip()[-400:]}
        results = json.loads(proc.stdout.strip().splitlines()[-1])
    out: dict[str, Any] = {"total": len(tests)}
    if results and results[-1][0] == "large":
        out["large_s"] = round(results.pop()[1], 3)
    out["passed"] = sum(1 for ok, _ in results if ok is True)
    fails = [(t[0], got) for t, (ok, got) in zip(tests, results, strict=False) if ok is not True]
    if fails:
        out["first_failure"] = f"args={json.dumps(fails[0][0])[:120]} got={fails[0][1][:120]}"
    return out


TEXT_UTILS = """import re

_WORD = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())
"""


def run_sql(query: str, setup: str) -> list[list[Any]] | str:
    q = re.sub(r"::\s*(numeric|decimal|float8?|double precision|real|int)", "", query, flags=re.I)
    q = re.sub(r"(?i)\bnumeric\b", "REAL", q)
    try:
        db = sqlite3.connect(":memory:")
        db.executescript(setup)
        rows = db.execute(q.strip().rstrip(";")).fetchall()
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"
    return [[round(v, 2) if isinstance(v, float) else v for v in r] for r in rows]


def has_keywords(text: str, groups: list[str]) -> bool:
    low = text.lower()
    return all(any(w in low for w in g.split("|")) for g in groups)


def grade(case: Case, run: Mapping[str, Any]) -> dict[str, Any]:
    reading = run.get("reading")
    kinds = [t["kind"] for t in reading["tasks"]] if reading else None
    g: dict[str, Any] = {"kinds_ok": kinds == list(case.kinds), "kinds": kinds}
    fast_parts = sections(run.get("fast", ""))
    code_text = run.get("code", "")
    code_parts = sections(code_text)
    checks: list[dict[str, Any]] = []
    for task in case.tasks:
        n = int(task["n"])
        part = fast_parts.get(n) or (run.get("fast", "") if len(case.kinds) == 1 else "")
        c: dict[str, Any] = {"n": n}
        if "choice" in task:
            got = chosen(part)
            c.update(kind="choice", ok=got == sorted(task["choice"]), got=got)
        elif "output" in task:
            # The code pass traces outputs; the fast pass's text when it did not run.
            blocks = code_blocks(code_parts.get(n) or code_text) or code_blocks(part)
            got = blocks[0][1].strip() if blocks else ""
            want = str(task["output"]).strip()
            c.update(
                kind="output",
                ok=[ln.rstrip() for ln in got.splitlines()] == want.splitlines(),
                got=got,
            )
        elif "keywords" in task:
            c.update(kind="keywords", ok=has_keywords(part, list(task["keywords"])))
            if case.kinds and case.kinds[n - 1] == "design":
                c["ok"] = c["ok"] and not code_blocks(part)
        elif "sql" in task:
            blocks = [
                code
                for lang, code in code_blocks(part)
                if lang in _LANGS["sql"] or "select" in code.lower()
            ]
            want = json.loads(task["sql"])
            got = run_sql(blocks[0], task["sql_setup"]) if blocks else "no query"
            c.update(kind="sql", ok=got == want, got=got)
        elif "function" in task:
            text = code_parts.get(n) or code_text
            picked = pick_code(text, str(task["function"])) or pick_code(
                code_text, str(task["function"])
            )
            if picked is None:
                c.update(kind="code", ok=False, error="no code block defines the function")
            else:
                tag, code = picked
                lang = str(task["language"])
                ran = run_code(lang, code, task)
                passed = ran["passed"] == ran["total"]
                fast_enough = ran.get("large_s", 0) <= LARGE_BUDGET_S
                c.update(
                    kind="code",
                    language_ok=language_ok(tag, code, lang),
                    ok=passed and fast_enough and language_ok(tag, code, lang),
                    **ran,
                )
        checks.append(c)
    g["checks"] = checks
    if not case.kinds:
        g["no_task_ok"] = bool(reading) and not reading["tasks"] and not run.get("code_ran")
    g["ok"] = g["kinds_ok"] and all(c["ok"] for c in checks) and g.get("no_task_ok", True)
    return g


# Running.


@dataclass
class Timer:
    t0: float = field(default_factory=time.monotonic)

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


async def solve(router: Any, settings: Any, case: Case, mode: str) -> dict[str, Any]:
    from copilot_core import screen
    from copilot_core.prompts import CallContext

    shots: list[screen.Shot] = []
    for spec in case.shots:
        data = spec.file.read_bytes()
        w, h = png_size(data)
        shots.append(screen.Shot(base64.b64encode(data).decode(), "image/png", w, h, spec.title))
    call = CallContext(recent=case.transcript) if mode == "interview" else None
    common: dict[str, Any] = {
        "answer_mode": mode, "call": call, "language": None,
        "default_language": settings.screen_default_language,
    }  # fmt: skip
    timer = Timer()
    run: dict[str, Any] = {"bytes": sum(s.size_bytes for s in shots), "calls": []}
    code_task: asyncio.Task[None] | None = None

    async def code_pass(reading: dict[str, Any] | None) -> None:
        req = screen.code_pass(
            "Alex", shots, reading, max_tokens=settings.screen_code_max_tokens, **common
        )
        run["code_ran"] = True
        run["code_start_ms"] = timer.ms()
        parts: list[str] = []
        async for chunk in router.stream("screen_code", req):
            if isinstance(chunk, str):
                if not parts:
                    run["code_first_ms"] = timer.ms()
                parts.append(chunk)
            else:
                run["calls"].append(chunk)
        run["code"] = "".join(parts)
        run["code_done_ms"] = timer.ms()

    if settings.screen_code_start == "with_fast":
        # The code pass at once, reading the screen itself, as the gateway starts it.
        code_task = asyncio.create_task(code_pass(None))
    req = screen.fast_pass("Alex", shots, max_tokens=settings.screen_fast_max_tokens, **common)
    reader = screen.ReadSplitter()
    shown: list[str] = []
    async for chunk in router.stream("screen", req):
        if not isinstance(chunk, str):
            run["calls"].append(chunk)
            continue
        was_done = reader.done
        text = reader.feed(chunk)
        if reader.done and not was_done:
            run["read_ms"] = timer.ms()
            run["reading"] = reader.reading
            needed = reader.reading is not None and screen.needs_code(reader.reading)
            if code_task is not None and reader.reading is not None and not needed:
                code_task.cancel()  # nothing on the screen needs it
                run["code_ran"] = False
                code_task = None
            elif code_task is None and needed:
                code_task = asyncio.create_task(code_pass(reader.reading))
        if text:
            if not shown:
                run["first_word_ms"] = timer.ms()
            shown.append(text)
    if rest := reader.flush():
        shown.append(rest)
    run["fast"] = "".join(shown)
    run["fast_done_ms"] = timer.ms()
    if code_task is None and screen.needs_code(reader.reading):
        code_task = asyncio.create_task(code_pass(None))
    if code_task is not None:
        await code_task
    run["code_start"] = settings.screen_code_start
    return run


def call_costs(settings: Any, calls: list[Any]) -> dict[str, Any]:
    return summarize_costs([{"cost_usd": call_cost(settings, done)} for done in calls])


def pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("interview", "assessment"):
        rs = [r for r in rows if r["mode"] == mode and "grade" in r]
        if not rs:
            continue
        checks = [c for r in rs for c in r["grade"]["checks"]]

        def rate(kind: str, checks: list[dict[str, Any]] = checks) -> str:
            got = [c for c in checks if c["kind"] == kind]
            return f"{sum(c['ok'] for c in got)}/{len(got)}" if got else "-"

        code_rs = [r for r in rs if r.get("code_done_ms") is not None]
        out[mode] = {
            "cases": f"{sum(r['grade']['ok'] for r in rs)}/{len(rs)}",
            "kinds": f"{sum(r['grade']['kinds_ok'] for r in rs)}/{len(rs)}",
            "choice": rate("choice"),
            "output": rate("output"),
            "code": rate("code"),
            "sql": rate("sql"),
            "keywords": rate("keywords"),
            "read_ms_p50": pct([r["read_ms"] for r in rs if "read_ms" in r], 0.5),
            "first_word_ms_p50": pct([r["first_word_ms"] for r in rs if "first_word_ms" in r], 0.5),
            "first_word_ms_p95": pct(
                [r["first_word_ms"] for r in rs if "first_word_ms" in r], 0.95
            ),
            "fast_done_ms_p50": pct([r["fast_done_ms"] for r in rs], 0.5),
            "code_first_ms_p50": pct(
                [r["code_first_ms"] for r in code_rs if "code_first_ms" in r], 0.5
            ),
            "code_done_ms_p50": pct([r["code_done_ms"] for r in code_rs], 0.5),
            "code_done_ms_p95": pct([r["code_done_ms"] for r in code_rs], 0.95),
            **summarize_costs(rs),
        }
    return out


def samples_md(label: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [f"# Coding eval: {label}", "", summary_table({label: summary}), ""]
    for r in rows:
        g = r.get("grade", {})
        verdict = "ok" if g.get("ok") else "FAIL"
        lines += [f"## {r['id']} ({r['mode']}): {verdict}", ""]
        lines.append(
            f"kinds {g.get('kinds')} · read {r.get('read_ms')} ms · first word "
            f"{r.get('first_word_ms')} ms · code done {r.get('code_done_ms')} ms"
        )
        for c in g.get("checks", []):
            detail = {k: v for k, v in c.items() if k not in ("n", "kind", "ok")}
            verdict = "ok" if c["ok"] else "FAIL"
            lines.append(f"- task {c['n']} {c['kind']}: {verdict} {json.dumps(detail)[:300]}")
        if r.get("error"):
            lines.append(f"- error: {r['error']}")
        lines += ["", "Fast pass:", "", "````", r.get("fast", "").strip(), "````", ""]
        if r.get("code"):
            lines += ["Code pass:", "", "````", r["code"].strip(), "````", ""]
    return "\n".join(lines)


def summary_table(summaries: Mapping[str, Mapping[str, Any]]) -> str:
    cols = ["cases", "kinds", "choice", "output", "code", "sql", "keywords", "read_ms_p50",
            "first_word_ms_p50", "first_word_ms_p95", "code_done_ms_p50", "code_done_ms_p95",
            "cost_usd"]  # fmt: skip
    head = "| label | mode | " + " | ".join(cols) + " |"
    rows = [head, "|" + " --- |" * (len(cols) + 2)]
    for label, s in summaries.items():
        for mode, m in s.items():
            if not isinstance(m, dict):
                continue
            vals = [str(cast(dict[str, Any], m).get(c, "-")) for c in cols]
            rows.append(f"| {label} | {mode} | " + " | ".join(vals) + " |")
    return "\n".join(rows)


async def cmd_run(args: argparse.Namespace) -> int:
    from copilot_core.dotenv import load_dotenv
    from copilot_gateway.config import Settings
    from copilot_gateway.router import build_router

    load_dotenv()
    settings = Settings()
    router = build_router(settings)
    await router.warm()
    chosen_cases = [c for c in cases() if not args.only or c.id in args.only]
    missing = [s.file.name for c in chosen_cases for s in c.shots if not s.file.exists()]
    if missing:
        sys.exit(f"missing shots {missing}: run `copilot-coding-eval render` first")
    sem = asyncio.Semaphore(args.concurrency)

    async def one(case: Case, mode: str, rep: int) -> dict[str, Any]:
        async with sem:
            row: dict[str, Any] = {"id": case.id, "mode": mode, "rep": rep}
            try:
                run = await solve(router, settings, case, mode)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
                print(f"{case.id:<18} {mode:<10} error {row['error']}")
                return row
            calls = run.pop("calls")
            row.update(run)
            row["usage"] = [
                {"model": d.model, "first_token_ms": d.first_token_ms, "attempt": d.attempt,
                 "input": d.usage.input_tokens, "cached": d.usage.cached_tokens,
                 "output": d.usage.output_tokens,
                 "cache_write_tokens": d.usage.cache_write_tokens,
                 "cache_write_30m_tokens": d.usage.cache_write_30m_tokens,
                 "cache_write_12h_tokens": d.usage.cache_write_12h_tokens,
                 "reasoning_tokens": d.usage.reasoning_tokens}
                for d in calls
            ]  # fmt: skip
            row.update(call_costs(settings, calls))
            row["models"] = sorted({d.model for d in calls})
            row["grade"] = await asyncio.to_thread(grade, case, run)
            g = row["grade"]
            print(
                f"{case.id:<18} {mode:<10} {'ok  ' if g['ok'] else 'FAIL'} kinds={g['kinds']} "
                f"first={row.get('first_word_ms')} code_done={row.get('code_done_ms')}"
            )
            return row

    jobs = [
        one(c, m, r)
        for r in range(args.reps)
        for c in chosen_cases
        for m in c.modes
        if m in args.modes
    ]
    rows = list(await asyncio.gather(*jobs))
    out = RESULTS / args.label
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    summary["models"] = sorted({m for r in rows for m in r.get("models", [])})
    (out / "runs.json").write_text(json.dumps(rows, indent=1, default=str), "utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=1), "utf-8")
    (out / "samples.md").write_text(samples_md(args.label, rows, summary), "utf-8")
    print(summary_table({args.label: summary}))
    print(f"\nCost: {format_cost(summarize_costs(rows))}  ->  {out}")
    return 0


def cmd_compare(labels: list[str]) -> str:
    summaries = {
        label: json.loads((RESULTS / label / "summary.json").read_text("utf-8")) for label in labels
    }
    return summary_table(summaries)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="copilot-coding-eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render")
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--only", nargs="*")
    r.add_argument("--modes", nargs="*", default=["interview", "assessment"])
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--concurrency", type=int, default=4)
    c = sub.add_parser("compare")
    c.add_argument("labels", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "render":
        render([s for case in cases() for s in case.shots])
        return 0
    if args.cmd == "compare":
        print(cmd_compare(args.labels))
        return 0
    return asyncio.run(cmd_run(args))


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
