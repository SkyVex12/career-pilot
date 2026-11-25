"""Trusted fixtures validate the offline evaluator; generated programs never run on the host."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from copilot_evals import live_answer_coding as coding

FIRST = """from collections import Counter
def first(text):
    counts = Counter(text)
    return next((char for char in text if counts[char] == 1), None)
"""
INTERVALS = """def merge(intervals):
    if any(a > b for a, b in intervals):
        raise ValueError('reversed')
    result = []
    for a, b in sorted(intervals):
        if a == b:
            continue
        if result and a < result[-1][1]:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result
"""
LRU = """from collections import OrderedDict
class Cache:
    def __init__(self, capacity, clock):
        self.capacity = capacity
        self.clock = clock
        self.data = OrderedDict()
    def purge(self):
        for key, (value, expiry) in list(self.data.items()):
            if expiry <= self.clock():
                del self.data[key]
    def get(self, key):
        self.purge()
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key][0]
    def put(self, key, value, ttl):
        self.purge()
        self.data.pop(key, None)
        if self.capacity == 0:
            return
        self.data[key] = (value, self.clock() + ttl)
        while len(self.data) > self.capacity:
            self.data.popitem(last=False)
"""
DEDUP = """class Dedup:
    def __init__(self, clock):
        self.clock = clock
        self.data = {}
    def seen(self, key):
        now = self.clock()
        self.data = {k: expiry for k, expiry in self.data.items() if expiry > now}
        duplicate = key in self.data
        self.data[key] = now + 60
        return duplicate
"""
WORKERS = """import asyncio
from itertools import islice
async def bounded(operation, items):
    it = iter(items)
    result = []
    while True:
        batch = list(islice(it, 8))
        if not batch:
            return result
        tasks = [asyncio.create_task(operation(x)) for x in batch]
        try:
            result.extend(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
"""
UTF8 = r"""import codecs
def records(chunks):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
    pending = ''
    for chunk in chunks:
        pending += decoder.decode(chunk)
        while '\n' in pending:
            line, pending = pending.split('\n', 1)
            yield line.removesuffix('\r')
    pending += decoder.decode(b'', final=True)
    if pending:
        yield pending
"""
ADAPTERS: dict[str, dict[str, Any]] = {
    "coverage-first_unique": {
        "entrypoint": "first",
        "result": "character",
        "missing": None,
        "character_unit": "code_points",
    },
    "stress-coding-intervals": {"entrypoint": "merge"},
    "stress-coding-expiring-lru": {
        "entrypoint": "Cache",
        "construct": {"args": ["capacity", "clock"]},
        "put": {"name": "put", "args": ["key", "value", "ttl"]},
        "get": {"name": "get", "args": ["key"]},
        "missing": {"kind": "value", "value": None},
        "expires_at_boundary": True,
        "boundary_policy_reviewed": True,
    },
    "stress-coding-dedup-window": {
        "entrypoint": "Dedup",
        "construct": {"args": ["clock"]},
        "observe": {"name": "seen", "args": ["key"]},
        "new_result": False,
        "duplicate_result": True,
        "expires_at_boundary": True,
        "boundary_policy_stated": True,
    },
    "stress-coding-bounded-workers": {
        "entrypoint": "bounded",
        "result": "awaitable",
        "call": {"args": ["operation", "items"]},
    },
    "stress-coding-utf8-stream": {
        "entrypoint": "records",
        "call": {"args": ["chunks"]},
        "invalid_bytes": "strict",
        "invalid_policy_stated": True,
        "delimiter_mode": "stripped",
        "delimiter_policy_reviewed": True,
    },
}
FIXTURES = list(zip(ADAPTERS, (FIRST, INTERVALS, LRU, DEDUP, WORKERS, UTF8), strict=True))


def entry(case: str, code: str) -> dict[str, Any]:
    answer = "```python\n" + code + "```"
    return {
        "answer_id": case + ":trusted-fixture",
        "case_id": case,
        "answer": answer,
        "answer_sha256": coding.fingerprint(answer),
        "selected_blocks": [0],
        "code_sha256": coding.fingerprint(code),
        "reviewed": True,
        "reviewer": "test-fixture",
        "adapter": copy.deepcopy(ADAPTERS[case]),
    }


@pytest.fixture(scope="module")
def container() -> None:
    if shutil.which("docker") is None:
        pytest.skip("requires local Docker; never falls back to host execution")
    result = subprocess.run(
        ["docker", "image", "inspect", coding.IMAGE], capture_output=True, timeout=10, check=False
    )
    if result.returncode:
        pytest.skip("pinned local Python image unavailable; no image pull")


@pytest.mark.parametrize(("case", "code"), FIXTURES)
def test_trusted_correct_fixtures_pass_isolated_suites(
    container: None, case: str, code: str
) -> None:
    result = coding.evaluate(entry(case, code))
    assert result["status"] == "passed", result
    assert result["checks"]
    assert all(check["passed"] for check in result["checks"])


@pytest.mark.parametrize(
    ("case", "code", "failed_check"),
    [
        (
            "coverage-first_unique",
            FIRST.replace("counts[char] == 1", "counts[char] > 1"),
            "first_unique_",
        ),
        ("stress-coding-intervals", INTERVALS.replace("a < result", "a <= result"), "intervals_2"),
        (
            "stress-coding-expiring-lru",
            LRU.replace("expiry <= self.clock()", "expiry < self.clock()"),
            "declared_expiry_equality",
        ),
        (
            "stress-coding-dedup-window",
            DEDUP.replace("self.data[key] = now + 60", "self.data.setdefault(key, now + 60)"),
            "retry_extends_retention_and_independent_ids",
        ),
        (
            "stress-coding-bounded-workers",
            "import asyncio\nasync def bounded(operation, items):\n"
            "    return await asyncio.gather(*(operation(x) for x in items))\n",
            "bounded_lazy_work",
        ),
        (
            "stress-coding-utf8-stream",
            UTF8.replace("decoder.decode(chunk)", "chunk.decode('utf-8')"),
            "utf8_split_",
        ),
    ],
)
def test_real_defects_fail_independent_checks(
    container: None,
    case: str,
    code: str,
    failed_check: str,
) -> None:
    result = coding.evaluate(entry(case, code))
    assert result["status"] == "failed", result
    assert any(
        not check["passed"] and check["name"].startswith(failed_check) for check in result["checks"]
    ), result


def test_obsolete_records_are_observed(container: None) -> None:
    code = DEDUP.replace("self.data = {}", "self.data = {}\n        self.obsolete = []").replace(
        "self.data[key] = now + 60",
        "self.data[key] = now + 60\n        self.obsolete.append((key, now))",
    )
    result = coding.evaluate(entry("stress-coding-dedup-window", code))
    assert result["status"] == "partially_scored", result
    assert result["functional_status"] == "passed"
    assert result["memory_review_required"] is True
    assert result["measurements"]["retained_container_slots_after_updates"]["8192"] > 20_000
    assert any(
        check["name"] == "obsolete_expiry_record_growth" and not check["passed"]
        for check in result["checks"]
    )


def test_undeclared_contract_is_unscored(container: None) -> None:
    sample = entry("coverage-first_unique", FIRST)
    sample["adapter"] = {"entrypoint": "first"}
    result = coding.evaluate(sample)
    assert result["status"] == "manual_review_required"


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.remove('x')",
        "from pathlib import Path",
        "open('x', 'w')",
        "eval('1')",
        "exec('1')",
        "getattr(object, '__class__')",
        "x.__class__",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "from codecs import open",
        "from collections import *",
        "import socket",
        "from subprocess import run",
        "import asyncio\nasyncio.get_running_loop()",
        "class A(metaclass=type):\n    pass",
    ],
)
def test_static_gate_rejects_unsafe_or_unsupported_code(code: str) -> None:
    assert coding.screen(code)


def test_review_and_hash_gates_do_not_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("container must not start before review")

    monkeypatch.setattr(coding, "isolated_run", forbidden)
    for field, value in (
        ("reviewed", False),
        ("reviewer", ""),
        ("code_sha256", "changed"),
        ("answer_sha256", "changed"),
        ("selected_blocks", [0, 0]),
    ):
        sample = entry("coverage-first_unique", FIRST)
        sample[field] = value
        assert coding.evaluate(sample)["status"] == "manual_review_required"


def test_extraction_preserves_original_code_and_requires_selection() -> None:
    answer = "Explanation\n```python\ndef f(x):\n return x\n```\n```python\nprint('demo')\n```"
    trial = {
        "id": "coverage-first_unique",
        "architecture": "hidden",
        "rep": 0,
        "events": [{"message": {"type": "answer.delta", "pass": "fast", "text": answer}}],
    }
    review = coding.prepare([trial])
    row = review["entries"][0]
    assert row["fences"][0]["code"] == "def f(x):\n return x\n"
    assert row["selected_blocks"] == []
    assert row["completed"] is False
    assert row["reviewed"] is False


def test_container_command_has_no_host_mounts_network_or_pull() -> None:
    cmd = coding.docker_command(coding.IMAGE, "test-name")
    for required in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pull=never",
        "--pids-limit=32",
        "--memory=256m",
        "--cpus=1",
        "--log-driver=none",
    ):
        assert required in cmd
    assert "-v" not in cmd
    assert "--mount" not in cmd
    assert "--privileged" not in cmd
    with pytest.raises(ValueError, match="immutable"):
        coding.docker_command("python:latest", "test")


def test_missing_docker_does_not_execute_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert coding.isolated_run({"code": "raise Exception()"})["status"] == "blocked"


def test_timeout_kills_and_removes_container(
    container: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "TIMEOUT_S", 1.5)
    result = coding.evaluate(
        entry("coverage-first_unique", "def first(s):\n    while True: pass\n")
    )
    assert result["status"] == "timeout"
    pending = subprocess.run(
        ["docker", "ps", "--filter", "name=career-pilot-coding-", "-q"],
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert pending.stdout.strip() == b""


def test_worker_refuses_direct_host_execution(tmp_path: Path) -> None:
    worker = Path(coding.__file__).with_name("live_answer_coding_runner.py")
    result = subprocess.run(
        [sys.executable, str(worker)],
        input=b"{}",
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert b"Refusing generated-code execution" in result.stderr


def test_review_file_round_trip() -> None:
    sample = entry("coverage-first_unique", FIRST)
    assert coding.selected_code(json.loads(json.dumps(sample))) == (FIRST, [])


@pytest.mark.parametrize("policy", ["replace", "ignore"])
def test_declared_non_strict_utf8_policy(container: None, policy: str) -> None:
    sample = entry(
        "stress-coding-utf8-stream", UTF8.replace("errors='strict'", f"errors='{policy}'")
    )
    sample["adapter"]["invalid_bytes"] = policy
    result = coding.evaluate(sample)
    assert result["status"] == "passed", result
    assert result["container_cleanup_confirmed"]


def test_first_unique_index_contract_is_not_forced_to_character(container: None) -> None:
    sample = entry(
        "coverage-first_unique",
        "def first(s):\n    return next((i for i, ch in enumerate(s) if s.count(ch) == 1), -1)\n",
    )
    sample["adapter"].update(result="index", missing=-1)
    assert coding.evaluate(sample)["status"] == "passed"


def test_dedup_inclusive_boundary_when_explicit(container: None) -> None:
    sample = entry("stress-coding-dedup-window", DEDUP.replace("expiry > now", "expiry >= now"))
    sample["adapter"]["expires_at_boundary"] = False
    assert coding.evaluate(sample)["status"] == "passed"


def test_output_flood_is_bounded_and_container_removed(container: None) -> None:
    code = "while True:\n    print('x' * 4096)\n"
    result = coding.evaluate(entry("coverage-first_unique", code))
    assert result["status"] == "output_limit"
    assert result["container_cleanup_confirmed"]


@pytest.mark.parametrize(
    "code",
    [
        "from asyncio import subprocess",
        "import asyncio as a\na.subprocess",
        "from typing import get_type_hints",
        "import codecs\ncodecs.open('x')",
        "import collections\ncollections._sys",
        "import asyncio\nasyncio.os.system('x')",
    ],
)
def test_stdlib_internal_exports_are_not_implicitly_allowed(code: str) -> None:
    assert coding.screen(code)


def test_reviewed_syntax_error_is_an_observed_program_failure() -> None:
    result = coding.evaluate(entry("coverage-first_unique", "def first(:\n    pass\n"))
    assert result["status"] == "failed"
    assert result["failure_kind"] == "syntax_error"
    assert result["reviewer"] == "test-fixture"
    assert result["adapter"]["result"] == "character"


def test_unreviewed_syntax_error_still_requires_extraction_review() -> None:
    sample = entry("coverage-first_unique", "def first(:\n    pass\n")
    sample["reviewed"] = False
    assert coding.evaluate(sample)["status"] == "manual_review_required"


def test_worker_api_refuses_generated_code_on_host() -> None:
    from copilot_evals.live_answer_coding_runner import run

    result = run({"code": "raise AssertionError('must not execute')"})
    assert result["status"] == "blocked"


@pytest.mark.parametrize(
    "failed",
    [
        {"status": "passed", "container_cleanup_confirmed": False},
        {"status": "blocked", "reason": "Docker unavailable"},
    ],
)
def test_sandbox_failure_stops_remaining_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    failed: dict[str, Any],
) -> None:
    calls: list[str] = []

    def fake(sample: dict[str, Any], image: str) -> dict[str, Any]:
        calls.append(sample["answer_id"])
        return failed

    monkeypatch.setattr(coding, "evaluate", fake)
    results = coding.evaluate_review([{"answer_id": "first"}, {"answer_id": "second"}])
    assert calls == ["first"]
    assert results[1]["status"] == "not_dispatched"
