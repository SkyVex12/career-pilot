"""The coding eval's offline parts: the cases, and the checks that run code and queries."""

import shutil
from typing import Any

import pytest
from copilot_evals.coding_eval import (
    cases,
    chosen,
    grade,
    pick_code,
    run_code,
    run_sql,
    sections,
    summarize,
)


def by_id() -> dict[str, Any]:
    return {c.id: c for c in cases()}


def test_cases_cover_every_kind_and_their_shots_exist() -> None:
    all_cases = cases()
    kinds = {k for c in all_cases for k in c.kinds}
    assert kinds >= {"code", "choice", "output", "fix", "sql", "short", "design"}
    got = by_id()
    assert len(got["long_2_shots"].shots) == 2
    assert len(got["multi_file"].shots) == 3
    assert got["code_py_js_asked"].modes == ("interview",)
    assert got["no_task"].kinds == ()
    for c in all_cases:
        for shot in c.shots:
            assert shot.file.exists(), shot.file


def test_sections_and_choices() -> None:
    text = "**Answer:** B. Hash table\nwhy\n\n### 2. Sorting\n**Answer:** C) Merge sort\n### 3. X\n"
    parts = sections(text)
    assert chosen(parts[1]) == ["B"]
    assert chosen(parts[2]) == ["C"]
    assert chosen("**Answers:** A. GET, C. PUT and D. DELETE") == ["A", "C", "D"]
    assert sections("no headings") == {1: "no headings"}


def test_python_code_runs_against_the_tests() -> None:
    task = {"function": "two", "tests": [["[1]", "2"], ["[2]", "4"]]}
    good = pick_code("```python\ndef two(x: int) -> int:\n    return x * 2\n```", "two")
    assert good is not None
    assert run_code("python", good[1], task) == {"total": 2, "passed": 2}
    bad = run_code("python", "def two(x):\n    return x + 1\n", task)
    assert bad["passed"] == 1
    assert "got=3" in bad["first_failure"]
    method = run_code(
        "python", "class Solution:\n    def two(self, x: List[int]):\n        return x * 2\n", task
    )
    assert method["passed"] == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_javascript_code_runs_against_the_tests() -> None:
    task = {"function": "pairs", "tests": [["[[1,2], 1]", "1"]]}
    code = "function pairs(nums, k) {\n  return nums.length - k;\n}\n"
    assert run_code("javascript", code, task)["passed"] == 1
    arrow = "const pairs = (nums, k) => 0;\n"
    assert run_code("javascript", arrow, task)["passed"] == 0


def test_sql_runs_on_sqlite_without_postgres_casts() -> None:
    setup = "CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1), (2);"
    assert run_sql("SELECT ROUND(AVG(x)::numeric, 2) FROM t;", setup) == [[1.5]]
    assert isinstance(run_sql("SELEC nothing", setup), str)


def test_grade_and_summary() -> None:
    case = by_id()["choice_single"]
    run = {
        "reading": {"tasks": [{"n": 1, "kind": "choice"}]},
        "fast": "**Answer:** B. O(log n)\nSift up.",
        "first_word_ms": 1200,
        "read_ms": 1100,
        "fast_done_ms": 1500,
    }
    g = grade(case, run)
    assert g["ok"]
    wrong = grade(case, {**run, "fast": "**Answer:** A. O(1)"})
    assert not wrong["ok"]
    no_task = grade(
        by_id()["no_task"], {"reading": {"tasks": [], "unreadable": "desktop"}, "fast": ""}
    )
    assert no_task["ok"]
    rows = [{"mode": "assessment", "grade": g, **run}]
    s = summarize(rows)["assessment"]
    assert s["cases"] == "1/1"
    assert s["choice"] == "1/1"
    assert s["first_word_ms_p50"] == 1200
