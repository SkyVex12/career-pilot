"""Trusted handwritten fixtures only. No generated program is executed on the host."""

import codecs
import collections
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
SRC = HERE / "copilot_evals" if (HERE / "copilot_evals").exists() else HERE / "src/copilot_evals"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SRC / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("private_coding_worker", "live_answer_coding_runner.py")
host = load("private_coding_host", "live_answer_coding.py")
DATA = json.loads((SRC / "live_answer_coding_contracts.json").read_text("utf-8"))["cases"]
ERROR = {"kind": "exception", "types": ["ValueError"]}


def checks(fn, *args):
    c = worker.Checks()
    fn(*args, c)
    return c


def novel(name, fn, adapter, vectors=None):
    c = worker.Checks()
    getattr(worker, name)(fn, adapter, c, DATA[name] if vectors is None else vectors)
    return c


def passed(c):
    assert not [x for x in c.results if x["passed"] is False], c.results


def split_path(text):
    out = []
    part = []
    escaped = False
    for char in text:
        if escaped:
            part.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            out.append("".join(part))
            part = []
        else:
            part.append(char)
    if escaped:
        raise ValueError("unpaired")
    return [*out, "".join(part)]


def test_escaped_path_good_and_actual_defect():
    adapter = {"call": {"args": ["path"]}, "rejection": ERROR}
    passed(novel("escaped_path", split_path, adapter))
    bad = novel("escaped_path", lambda s: s.split("."), adapter)
    assert any(x["passed"] is False for x in bad.results)


def test_path_declared_return_sentinel_is_supported():
    def sentinel(text):
        try:
            return split_path(text)
        except ValueError:
            return None

    passed(
        novel(
            "escaped_path",
            sentinel,
            {"call": {"args": ["path"]}, "rejection": {"kind": "value", "value": None}},
        )
    )


def merge_streams(streams):
    import heapq

    yield from heapq.merge(*streams)


def test_lazy_merge_and_eager_fanout_defect():
    adapter = {"call_shape": "streams", "call": {"args": ["streams"]}}
    passed(novel("lazy_merge", merge_streams, adapter))

    def eager(streams):
        return iter(sorted(x for stream in streams for x in stream))

    c = novel("lazy_merge", eager, adapter)
    assert any(x["passed"] is False for x in c.results)


def test_merge_variadic_interface():
    passed(novel("lazy_merge", lambda *streams: merge_streams(streams), {"call_shape": "variadic"}))


def encode(text):
    import itertools

    return [(char, len(list(group))) for char, group in itertools.groupby(text)]


def decode(records):
    parts = []
    for char, count in records:
        if not isinstance(count, int) or count <= 0:
            raise ValueError("count")
        parts.append(char * count)
    return "".join(parts)


RLE = {
    "entrypoint": "encode",
    "decode_entrypoint": "decode",
    "encode_call": {"args": ["text"]},
    "decode_call": {"args": ["records"]},
    "character_unit": "code_points",
    "rejection": ERROR,
}


def test_rle_unicode_invalid_counts_and_diagnostic_bool():
    c = worker.Checks()
    worker.run_length({"encode": encode, "decode": decode}, RLE, c, DATA["run_length"])
    passed(c)
    assert c.summary()["status"] == "passed"
    assert c.summary()["scope_counts"]["diagnostic"]["unscored"] == 1


def test_rle_wrong_decode_fails():
    c = worker.Checks()
    worker.run_length(
        {"encode": encode, "decode": lambda pairs: "".join(ch for ch, n in pairs)},
        RLE,
        c,
        DATA["run_length"],
    )
    assert any(x["passed"] is False for x in c.results)


def topo(graph):
    degree = {x: 0 for x in graph}
    for values in graph.values():
        for x in values:
            degree[x] = degree.get(x, 0) + 1
    out = []
    while degree:
        ready = [x for x, n in degree.items() if n == 0]
        if not ready:
            raise ValueError("cycle")
        item = min(ready)
        out.append(item)
        del degree[item]
        for target in graph.get(item, []):
            degree[target] -= 1
    return out


DAG = {
    "call": {"args": ["graph"]},
    "orientation": "successors",
    "orientation_reviewed": True,
    "cycle_error": ERROR,
}


def test_topo_direction_isolates_lexical_and_cycles():
    passed(novel("dependency_order", topo, DAG))
    bad = novel("dependency_order", lambda graph: sorted(graph), DAG)
    assert any(x["passed"] is False for x in bad.results)


def test_prerequisite_representation_is_allowed_only_when_declared():
    def dependencies(graph):
        successors = {x: [] for x in graph}
        for node, deps in graph.items():
            for dep in deps:
                successors[dep].append(node)
        return topo(successors)

    passed(novel("dependency_order", dependencies, {**DAG, "orientation": "prerequisites"}))
    bad = novel("dependency_order", dependencies, DAG)
    assert any(x["passed"] is False for x in bad.results)
    with pytest.raises(worker.ContractError):
        novel("dependency_order", topo, {**DAG, "orientation_reviewed": False})


def invalidation(root, reverse, include=False):
    seen = {root}
    pending = list(reverse.get(root, []))
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(reverse.get(node, []))
    if not include:
        seen.remove(root)
    return sorted(seen)


INV = {
    "call": {"args": ["root", "reverse"]},
    "root_policy": "exclude_root",
    "root_policy_reviewed": True,
}


@pytest.mark.parametrize("include", [False, True])
def test_invalidation_root_policy_and_cycles(include):
    policy = "include_root" if include else "exclude_root"
    passed(
        novel(
            "dependency_invalidation",
            lambda root, rev: invalidation(root, rev, include),
            {**INV, "root_policy": policy},
        )
    )


def test_invalidation_unknown_root_membership_is_not_silent_pass():
    c = novel("dependency_invalidation", invalidation, {**INV, "root_policy": None})
    passed(c)
    assert c.summary()["status"] == "partially_scored"
    assert c.summary()["scope_counts"]["declared_policy"]["unscored"] == 7
    assert c.summary()["scope_counts"]["mandatory"]["passed"] == 7


def test_invalidation_wrong_direction_fails_without_transpose_adapter():
    def wrong(root, mapping):
        reverse = {}
        for node, targets in mapping.items():
            for target in targets:
                reverse.setdefault(target, []).append(node)
        return invalidation(root, reverse)

    assert any(x["passed"] is False for x in novel("dependency_invalidation", wrong, INV).results)


def med(values, policy):
    values = sorted(values)
    n = len(values)
    if n % 2:
        return values[n // 2]
    if policy == "lower":
        return values[n // 2 - 1]
    if policy == "upper":
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2


def rolling(stream, k, even="mean", warmup="full_only", grow=False):
    queue = collections.deque(maxlen=k)
    history = []
    for value in stream:
        queue.append(value)
        if grow:
            history.append(value)
        if warmup == "full_only" and len(queue) < k:
            continue
        yield None if warmup == "sentinel_until_full" and len(queue) < k else med(queue, even)


MED = {
    "call": {"args": ["stream", "k"]},
    "interface": "stream",
    "even": "mean",
    "even_policy_stated": True,
    "warmup": "full_only",
    "warmup_reviewed": True,
}


@pytest.mark.parametrize("even", ["mean", "lower", "upper"])
@pytest.mark.parametrize("warmup", ["partial", "full_only", "sentinel_until_full"])
def test_median_all_frozen_policy_families(even, warmup):
    vectors = [x for x in DATA["sliding_median"] if x["scope"] != "instrumented_memory_witness"]
    # Memory probes are bounded, but run once below; semantic cases cover all nine policy families.
    c = novel(
        "sliding_median",
        lambda stream, k: rolling(stream, k, even, warmup),
        {**MED, "even": even, "warmup": warmup, "sentinel": None},
        vectors,
    )
    passed(c)


def test_median_unknown_policy_stays_unscored():
    with pytest.raises(worker.ContractError):
        novel("sliding_median", rolling, {**MED, "warmup_reviewed": False})
    with pytest.raises(worker.ContractError):
        novel("sliding_median", rolling, {**MED, "even_policy_stated": False})


def test_median_growth_counterexample_is_detected():
    c = novel("sliding_median", lambda stream, k: rolling(stream, k, grow=True), MED)
    assert any(x["name"].startswith("state_growth") and x["passed"] is False for x in c.results)
    assert c.summary()["functional_status"] == "passed"
    assert c.summary()["memory_review_required"] is True
    assert c.summary()["status"] == "partially_scored"


def test_median_wrong_even_fraction_fails():
    def floor(stream, k):
        for value in rolling(stream, k):
            yield int(value)

    c = novel("sliding_median", floor, MED)
    assert any(x["name"].startswith("median-") and x["passed"] is False for x in c.results)


def merge_intervals(values):
    if any(a > b for a, b in values):
        raise ValueError("reversed")
    out = []
    for a, b in sorted(values):
        if a == b:
            continue
        if out and a < out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out[::-1]  # Unsorted outer output is valid under original question.


def test_intervals_do_not_impose_sorted_output():
    passed(checks(worker.intervals, merge_intervals))


class Cache:
    def __init__(self, capacity, clock, at=True):
        self.capacity = capacity
        self.clock = clock
        self.at = at
        self.data = collections.OrderedDict()

    def purge(self):
        now = self.clock()
        self.data = collections.OrderedDict(
            (k, v) for k, v in self.data.items() if v[1] > now or (not self.at and v[1] == now)
        )

    def get(self, key):
        self.purge()
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key][0]

    def put(self, key, value, ttl):
        self.purge()
        self.data.pop(key, None)
        if not self.capacity:
            return
        self.data[key] = (value, self.clock() + ttl)
        while len(self.data) > self.capacity:
            self.data.popitem(last=False)


Cache.__module__ = "candidate"
CACHE = {
    "construct": {"args": ["capacity", "clock"]},
    "put": {"name": "put", "args": ["key", "value", "ttl"]},
    "get": {"name": "get", "args": ["key"]},
    "missing": {"kind": "value", "value": None},
}


@pytest.mark.parametrize("at", [True, False])
def test_lru_both_equality_policies(at):
    passed(
        checks(
            worker.expiring_lru,
            lambda capacity, clock: Cache(capacity, clock, at),
            {**CACHE, "expires_at_boundary": at, "boundary_policy_reviewed": True},
        )
    )


def test_lru_undeclared_boundary_preserves_core_but_unscored():
    c = checks(worker.expiring_lru, Cache, CACHE)
    passed(c)
    assert c.summary()["status"] == "partially_scored"


async def serial_map(operation, items):
    return [await operation(item) for item in items]


async def serial_iterator(operation, items):
    for item in items:
        yield await operation(item)


@pytest.mark.parametrize(
    ("fn", "kind"), [(serial_map, "awaitable"), (serial_iterator, "async_iterator")]
)
def test_workers_allow_both_declared_result_interfaces(fn, kind):
    passed(
        checks(
            worker.bounded_workers, fn, {"call": {"args": ["operation", "items"]}, "result": kind}
        )
    )


def records(chunks, preserve=False):
    decoder = codecs.getincrementaldecoder("utf8")()
    pending = ""
    for chunk in chunks:
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line + "\n" if preserve else line.removesuffix("\r")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


UTF = {
    "call": {"args": ["chunks"]},
    "invalid_bytes": "strict",
    "invalid_policy_stated": True,
    "delimiter_mode": "stripped",
    "delimiter_policy_reviewed": True,
}


@pytest.mark.parametrize("preserve", [False, True])
def test_utf8_both_delimiter_representations(preserve):
    passed(
        checks(
            worker.utf8_stream,
            lambda chunks: records(chunks, preserve),
            {**UTF, "delimiter_mode": "preserved" if preserve else "stripped"},
        )
    )


def test_utf8_missing_invalid_policy_is_unscored():
    c = checks(worker.utf8_stream, records, {**UTF, "invalid_policy_stated": False})
    passed(c)
    assert c.summary()["status"] == "partially_scored"


def test_first_unique_ascii_without_unicode_assumption():
    def fn(text):
        return next((c for c in text if text.count(c) == 1), None)

    c = checks(worker.first_unique, fn, {"result": "character", "missing": None})
    passed(c)
    assert c.summary()["status"] == "partially_scored"


def test_host_worker_guard_refuses_candidate_execution():
    # Invalid code would raise if run() compiled it; isolation guard must return first.
    result = worker.run(
        {
            "code": 'raise AssertionError("should never run")',
            "adapter": {"entrypoint": "x"},
            "case": "first_unique",
        }
    )
    assert result["status"] == "blocked"


def test_prepare_includes_missing_primary_and_all_novel_cases():
    ids = [x for x in host.CASES if x.startswith("novel-")]
    review = host.prepare(
        [{"id": cid, "architecture": "test", "rep": 0, "events": []} for cid in ids]
    )
    assert len(review["entries"]) == 6
    assert all(x["delivery"] == "no_answer" and x["code_requested"] for x in review["entries"])
    assert all(x["selected_blocks"] == [] and not x["reviewed"] for x in review["entries"])


def test_static_injection_and_adapter_code_rejected():
    assert host.screen('import os\nos.system("anything")')
    assert host.screen('open("anywhere", "w")')
    assert host.screen('__import__("socket")')
    with pytest.raises(worker.ContractError):
        worker.invoke(lambda: None, {"args": [], "code": "evil"}, {})
    with pytest.raises(worker.ContractError):
        worker.invoke(lambda: None, {"args": ["unavailable"]}, {})


def test_pinned_docker_bootstrap_no_mount_or_network():
    cmd = host.docker_command(host.IMAGE, "trusted-fixture")
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--pull=never" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert not any(x in ("-v", "--volume", "--mount", "--privileged") for x in cmd)
    assert cmd[-1] == host.BOOTSTRAP
    assert len(cmd[-1]) < 400
    assert "sys.stdin" in host.BOOTSTRAP
    with pytest.raises(ValueError, match="immutable image"):
        host.docker_command("python:latest", "trusted-fixture")


def test_unscored_checks_cannot_become_pass():
    c = worker.Checks()
    c.unscored("policy", "unknown")
    assert c.summary()["status"] == "manual_review_required"
    c.check("core", lambda: None)
    assert c.summary()["status"] == "partially_scored"
    c.check("defect", lambda: worker.require(False, "bad"))
    assert c.summary()["status"] == "failed"


def test_contract_fingerprint_failure_blocks_before_docker(monkeypatch):
    original = Path.read_bytes

    def changed(path):
        if path.name == "live_answer_coding_contracts.json":
            return b"{}"
        return original(path)

    def no_dispatch(*args, **kwargs):
        raise AssertionError("Docker must not start after contract mismatch")

    monkeypatch.setattr(Path, "read_bytes", changed)
    monkeypatch.setattr(host.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(host.subprocess, "Popen", no_dispatch)
    result = host.isolated_run({"case": "escaped_path", "code": "", "adapter": {}})
    assert result == {"status": "blocked", "reason": "prospective contract fingerprint mismatch"}


def test_changed_answer_or_fence_hash_is_rejected_without_repair():
    code = "def first(text):\n    return None\n"
    answer = "```python\n" + code + "```"
    entry = {
        "case_id": "coverage-first_unique",
        "answer": answer,
        "answer_sha256": host.fingerprint(answer),
        "selected_blocks": [0],
        "code_sha256": host.fingerprint(code),
        "reviewed": True,
        "reviewer": "trusted-test",
        "adapter": {"entrypoint": "first"},
    }
    selected, errors = host.selected_code(entry)
    assert selected == code
    assert errors == []
    _, changed = host.selected_code({**entry, "code_sha256": "wrong"})
    assert "selected code fingerprint must be reviewed" in changed
    _, changed = host.selected_code({**entry, "answer": answer + "altered"})
    assert changed == ["answer fingerprint mismatch"]


class Rolling:
    def __init__(self, k):
        self.values = collections.deque(maxlen=k)

    def add(self, value):
        self.values.append(value)

    def current(self):
        return med(self.values, "mean")


Rolling.__module__ = "candidate"


def test_median_stateful_interface_and_bounded_state():
    adapter = {
        **MED,
        "interface": "stateful",
        "construct": {"args": ["k"]},
        "update": {"name": "add", "args": ["value"]},
        "query": {"name": "current", "args": []},
        "emission": "query",
    }
    c = novel("sliding_median", Rolling, adapter)
    passed(c)
    assert c.summary()["functional_status"] == "passed"
    assert c.summary()["memory_review_required"] is False


class Dedup:
    def __init__(self, clock):
        self.clock = clock
        self.data = {}

    def observe(self, key):
        now = self.clock()
        self.data = {k: expiry for k, expiry in self.data.items() if expiry > now}
        duplicate = key in self.data
        self.data[key] = now + 60
        return duplicate


Dedup.__module__ = "candidate"


def test_dedup_missing_boundary_preserves_core_checks():
    adapter = {
        "construct": {"args": ["clock"]},
        "observe": {"name": "observe", "args": ["key"]},
        "new_result": False,
        "duplicate_result": True,
    }
    c = checks(worker.dedup_window, Dedup, adapter)
    passed(c)
    assert c.summary()["status"] == "partially_scored"
    assert c.summary()["scope_counts"]["declared_policy"]["unscored"] == 1


def test_exception_type_adapter_rejects_executable_expression():
    with pytest.raises(worker.ContractError, match="explicit exception type names"):
        worker.declared_error(lambda: None, {"kind": "exception", "types": ["__import__('os')"]})


def test_intervals_generator_validation_is_consumed():
    def generated(values):
        yield from merge_intervals(values)

    passed(checks(worker.intervals, generated))


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_reviewed_contract_accepts_git_line_endings(monkeypatch, newline):
    original = Path.read_bytes

    def converted(path):
        value = original(path)
        return value.replace(b"\r\n", b"\n").replace(b"\n", newline)

    def stop_before_dispatch(*args, **kwargs):
        raise ValueError("contract accepted; no Docker dispatch")

    monkeypatch.setattr(Path, "read_bytes", converted)
    monkeypatch.setattr(host.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(host, "docker_command", stop_before_dispatch)
    with pytest.raises(ValueError, match="contract accepted"):
        host.isolated_run({"case": "escaped_path", "code": "", "adapter": {}})
