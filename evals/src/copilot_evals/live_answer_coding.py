"""Review unchanged Python answers, then test only in a disposable networkless container.

    python -m copilot_evals.live_answer_coding prepare --trials trials.json --out review.json
    python -m copilot_evals.live_answer_coding run --review review.json --out results.json

Reviewers select fences and declare interfaces; this never repairs code or guesses contracts.
AST screening is defense in depth, not a Python security sandbox. No host execution fallback.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

IMAGE = "sha256:85d4cb1afa769a7338e095b927bee941cf5ec92266c7424b3f6c0f2748567248"
CONTRACTS_SHA256 = "04a488b6c337631c8cd3b92dc25362c8a2e8c37a0bd1eeaa7c4d50a4baf805f9"
BOOTSTRAP = (
    "import json,sys;"
    " envelope=json.load(sys.stdin);"
    ' ns={"__name__":"trusted_grader"};'
    ' exec(compile(envelope["worker"],"<trusted-grader>","exec"),ns);'
    ' print(json.dumps(ns["run"](envelope["payload"])))'
)

MAX_CODE_BYTES = 65_536
MAX_OUTPUT_BYTES = 65_536
TIMEOUT_S = 15
CASES = {
    "novel-mosaic-coding-dependency-invalidation": "dependency_invalidation",
    "novel-mosaic-coding-sliding-median": "sliding_median",
    "novel-mosaic-coding-dependency-order": "dependency_order",
    "novel-cedar-coding-run-length": "run_length",
    "novel-cedar-coding-lazy-merge": "lazy_merge",
    "novel-cedar-coding-escaped-path": "escaped_path",
    "coverage-first_unique": "first_unique",
    "stress-coding-expiring-lru": "expiring_lru",
    "stress-coding-intervals": "intervals",
    "stress-coding-bounded-workers": "bounded_workers",
    "stress-coding-dedup-window": "dedup_window",
    "stress-coding-utf8-stream": "utf8_stream",
}
FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
MODULE_EXPORTS = {
    "__future__": {"annotations"},
    "asyncio": {
        "sleep",
        "gather",
        "create_task",
        "ensure_future",
        "wait",
        "wait_for",
        "as_completed",
        "TaskGroup",
        "Task",
        "Semaphore",
        "BoundedSemaphore",
        "Queue",
        "QueueEmpty",
        "QueueFull",
        "Event",
        "Lock",
        "Condition",
        "Future",
        "CancelledError",
        "TimeoutError",
        "FIRST_COMPLETED",
        "FIRST_EXCEPTION",
        "ALL_COMPLETED",
        "timeout",
        "current_task",
    },
    "bisect": {"bisect", "bisect_left", "bisect_right", "insort", "insort_left", "insort_right"},
    "codecs": {"getincrementaldecoder", "iterdecode", "decode"},
    "collections": {"Counter", "OrderedDict", "deque", "defaultdict", "namedtuple", "abc"},
    "collections.abc": {
        "Iterable",
        "Iterator",
        "AsyncIterable",
        "AsyncIterator",
        "Callable",
        "Sequence",
        "Mapping",
        "MutableMapping",
        "Awaitable",
        "Coroutine",
    },
    "dataclasses": {"dataclass", "field", "asdict", "replace"},
    "heapq": {
        "heappush",
        "heappop",
        "heapify",
        "heapreplace",
        "heappushpop",
        "nlargest",
        "nsmallest",
        "merge",
    },
    "itertools": {
        "islice",
        "chain",
        "count",
        "repeat",
        "groupby",
        "tee",
        "cycle",
        "pairwise",
        "takewhile",
        "dropwhile",
        "zip_longest",
        "accumulate",
    },
    "math": {
        "inf",
        "nan",
        "isfinite",
        "isnan",
        "isinf",
        "ceil",
        "floor",
        "sqrt",
        "log",
        "log2",
        "exp",
        "trunc",
    },
    "time": {"monotonic", "perf_counter"},
    "typing": {
        "Any",
        "Callable",
        "Iterable",
        "Iterator",
        "AsyncIterable",
        "AsyncIterator",
        "TypeVar",
        "Generic",
        "Optional",
        "Union",
        "List",
        "Dict",
        "Tuple",
        "Set",
        "Deque",
        "Sequence",
        "Mapping",
        "MutableMapping",
        "Awaitable",
        "Coroutine",
        "TypeAlias",
        "Protocol",
        "Self",
        "Literal",
        "NamedTuple",
    },
}
IMPORTS = set(MODULE_EXPORTS)

DENIED = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "io",
    "pathlib",
    "builtins",
    "importlib",
    "ctypes",
    "inspect",
    "marshal",
    "pickle",
    "posix",
    "nt",
    "system",
    "popen",
    "Popen",
    "fork",
    "spawn",
    "execv",
    "execve",
    "read_bytes",
    "write_bytes",
    "read_text",
    "write_text",
    "open",
    "input",
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "help",
    "breakpoint",
    "exit",
    "quit",
    "get_running_loop",
    "get_event_loop",
    "set_event_loop",
    "new_event_loop",
    "run_in_executor",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "open_connection",
    "start_server",
    "open_unix_connection",
    "start_unix_server",
    "to_thread",
    "connect_read_pipe",
    "connect_write_pipe",
    "create_connection",
    "create_server",
    "create_datagram_endpoint",
    "subprocess_exec",
    "subprocess_shell",
    "sendfile",
    "add_reader",
    "add_writer",
    "read",
    "readinto",
    "write",
    "load",
    "loads",
    "dump",
    "dumps",
    "register",
}
SPECIAL_METHODS = {"__init__", "__len__", "__call__", "__iter__", "__next__"}
# Explicit nodes only; future syntax is reviewed rather than silently enabled.
NODES = {
    "Module",
    "Import",
    "ImportFrom",
    "alias",
    "FunctionDef",
    "AsyncFunctionDef",
    "ClassDef",
    "arguments",
    "arg",
    "Return",
    "Assign",
    "AnnAssign",
    "AugAssign",
    "Expr",
    "Pass",
    "Delete",
    "If",
    "For",
    "AsyncFor",
    "While",
    "Break",
    "Continue",
    "Raise",
    "Try",
    "ExceptHandler",
    "Assert",
    "With",
    "AsyncWith",
    "withitem",
    "Await",
    "Yield",
    "YieldFrom",
    "Name",
    "Load",
    "Store",
    "Del",
    "Attribute",
    "Subscript",
    "Slice",
    "Call",
    "keyword",
    "Constant",
    "List",
    "Tuple",
    "Set",
    "Dict",
    "ListComp",
    "SetComp",
    "DictComp",
    "GeneratorExp",
    "comprehension",
    "IfExp",
    "Lambda",
    "BoolOp",
    "BinOp",
    "UnaryOp",
    "Compare",
    "JoinedStr",
    "FormattedValue",
    "Starred",
    "NamedExpr",
    "Nonlocal",
    "And",
    "Or",
    "Add",
    "Sub",
    "Mult",
    "Div",
    "FloorDiv",
    "Mod",
    "Pow",
    "MatMult",
    "LShift",
    "RShift",
    "BitOr",
    "BitXor",
    "BitAnd",
    "Invert",
    "Not",
    "UAdd",
    "USub",
    "Eq",
    "NotEq",
    "Lt",
    "LtE",
    "Gt",
    "GtE",
    "Is",
    "IsNot",
    "In",
    "NotIn",
}


ADAPTER_TEMPLATES: dict[str, dict[str, Any]] = {
    "first_unique": {"entrypoint": "", "result": "character OR index", "missing": None},
    "intervals": {"entrypoint": ""},
    "expiring_lru": {
        "entrypoint": "",
        "construct": {"args": ["capacity", "clock"]},
        "put": {"name": "put", "args": ["key", "value", "ttl"]},
        "get": {"name": "get", "args": ["key"]},
        "missing": {"kind": "value", "value": None},
    },
    "bounded_workers": {"entrypoint": "", "call": {"args": ["operation", "items"]}},
    "dedup_window": {
        "entrypoint": "",
        "construct": {"args": ["clock"]},
        "observe": {"name": "", "args": ["key"]},
        "new_result": None,
        "duplicate_result": None,
        "expires_at_boundary": None,
        "boundary_policy_stated": False,
    },
    "utf8_stream": {
        "entrypoint": "",
        "call": {"args": ["chunks"]},
        "invalid_bytes": "strict OR replace OR ignore",
        "invalid_policy_stated": False,
    },
}


ADAPTER_TEMPLATES.update(
    {
        "escaped_path": {
            "entrypoint": "",
            "call": {"args": ["path"]},
            "rejection": {"kind": "exception", "types": []},
        },
        "lazy_merge": {"entrypoint": "", "call_shape": None, "call": {"args": ["streams"]}},
        "run_length": {
            "entrypoint": "",
            "decode_entrypoint": "",
            "encode_call": {"args": ["text"]},
            "decode_call": {"args": ["records"]},
            "character_unit": None,
            "rejection": {"kind": "exception", "types": []},
        },
        "dependency_order": {
            "entrypoint": "",
            "call": {"args": ["graph"]},
            "orientation": None,
            "orientation_reviewed": False,
            "cycle_error": {"kind": "exception", "types": []},
        },
        "sliding_median": {
            "entrypoint": "",
            "interface": None,
            "call": {"args": ["stream", "k"]},
            "even": None,
            "even_policy_stated": False,
            "warmup": None,
            "warmup_reviewed": False,
        },
        "dependency_invalidation": {
            "entrypoint": "",
            "call": {"args": ["root", "reverse"]},
            "root_policy": None,
            "root_policy_reviewed": False,
        },
    }
)
ADAPTER_TEMPLATES["first_unique"]["character_unit"] = None
ADAPTER_TEMPLATES["expiring_lru"].update(expires_at_boundary=None, boundary_policy_reviewed=False)
ADAPTER_TEMPLATES["bounded_workers"]["result"] = None
ADAPTER_TEMPLATES["utf8_stream"].update(delimiter_mode=None, delimiter_policy_reviewed=False)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def blocks(text: str) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "language": match[1].strip().lower(),
            "code": match[2],
            "sha256": fingerprint(match[2]),
        }
        for i, match in enumerate(FENCE.finditer(text))
    ]


def screen(code: str) -> list[str]:
    """Conservative static exclusions; a clean result does NOT authorize host execution."""
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return ["code exceeds size limit"]
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return [f"invalid Python: {type(exc).__name__}"]
    nodes = list(ast.walk(tree))
    if len(nodes) > 12_000:
        return ["AST exceeds size limit"]
    errors: set[str] = set()
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
    for node in nodes:
        kind = type(node).__name__
        if kind not in NODES:
            errors.add(f"unsupported syntax: {kind}")
        if isinstance(node, ast.Import) and any(alias.name not in IMPORTS for alias in node.names):
            errors.add("import outside explicit standard-library allowlist")
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in IMPORTS:
                errors.add("import outside explicit standard-library allowlist")
            if any(a.name not in MODULE_EXPORTS.get(node.module or "", set()) for a in node.names):
                errors.add("unsafe or unsupported imported symbol")
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in aliases
                and node.attr not in MODULE_EXPORTS.get(aliases[node.value.id], set())
            ):
                errors.add("unsupported standard-library module attribute")
            if node.attr.startswith("_") and not (
                isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            ):
                errors.add("private attribute access requires manual review")
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in DENIED or name.startswith("__"):
                errors.add(f"restricted name or attribute: {name}")
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("__")
            and node.name not in SPECIAL_METHODS
        ):
            errors.add("unsupported special method")
        if isinstance(node, ast.ClassDef) and node.keywords:
            errors.add("class keyword/metaclass requires manual review")
    return sorted(errors)


def prepare(trials: list[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for trial in trials:
        case_id = trial.get("id")
        if case_id not in CASES:
            continue
        for pass_ in ("fast", "deep"):
            messages = [
                e["message"] for e in trial.get("events", []) if e["message"].get("pass") == pass_
            ]
            text = "".join(m.get("text", "") for m in messages if m["type"] == "answer.delta")
            if pass_ == "deep" and not messages:
                continue
            entries.append(
                {
                    "answer_id": (
                        f"{case_id}:{trial.get('architecture')}:{trial.get('rep')}:{pass_}"
                    ),
                    "case_id": case_id,
                    "pass": pass_,
                    "answer": text,
                    "answer_sha256": fingerprint(text),
                    "code_requested": case_id != "coverage-first_unique",
                    "delivery": "text_present" if text else "no_answer",
                    "completed": any(m["type"] == "answer.done" for m in messages),
                    "fences": blocks(text),
                    "selected_blocks": [],
                    "code_sha256": None,
                    "reviewed": False,
                    "reviewer": "",
                    "adapter": json.loads(json.dumps(ADAPTER_TEMPLATES[CASES[case_id]])),
                }
            )
    return {
        "schema": 1,
        "entries": entries,
        "review_instructions": "Select original Python fences in original order; record their "
        "concatenated SHA256 (one newline separator), reviewer, and declared adapter. Do not "
        "repair programs. Empty or ambiguous template fields need review. Call args/kwargs map "
        "parameter positions/names to the listed test tokens (clock, capacity, ttl, key, value; "
        "operation, items, optional limit=8; window=60; chunks, optional invalid_bytes). Missing "
        "cache values use {kind: value, value: ...} or {kind: KeyError}. Unsupported contracts "
        "remain manual_review_required.",
    }


def selected_code(entry: dict[str, Any]) -> tuple[str, list[str]]:
    errors: list[str] = []
    text = entry.get("answer", "")
    if not isinstance(text, str) or fingerprint(text) != entry.get("answer_sha256"):
        return "", ["answer fingerprint mismatch"]
    available = blocks(text)
    raw_indices = entry.get("selected_blocks")
    if not isinstance(raw_indices, list):
        return "", ["explicit ordered fence selection required"]
    indices = cast(list[Any], raw_indices)
    if (
        not indices
        or any(type(i) is not int for i in indices)
        or indices != sorted(set(indices))
        or any(i < 0 or i >= len(available) for i in indices)
    ):
        return "", ["explicit ordered fence selection required"]
    chosen = [available[i] for i in cast(list[int], indices)]
    if any(b["language"] not in ("python", "py", "python3", "") for b in chosen):
        errors.append("only Python is supported")
    code = "\n".join(b["code"] for b in chosen)
    if fingerprint(code) != entry.get("code_sha256"):
        errors.append("selected code fingerprint must be reviewed")
    reviewer = entry.get("reviewer")
    if entry.get("reviewed") is not True or not isinstance(reviewer, str) or not reviewer.strip():
        errors.append("explicit interface and extraction review required")
    if entry.get("case_id") not in CASES:
        errors.append("unsupported case")
    if not isinstance(entry.get("adapter"), dict):
        errors.append("declarative adapter required")
    return code, errors + screen(code)


def docker_command(image: str, name: str) -> list[str]:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("A locally inspected immutable image ID is required")
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=32",
        "--memory=256m",
        "--memory-swap=256m",
        "--cpus=1",
        "--ulimit",
        "nofile=64:64",
        "--log-driver=none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=8m",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "CAREER_PILOT_CODING_ISOLATED=1",
        "--entrypoint=/usr/local/bin/python",
        "-i",
        image,
        "-I",
        "-B",
        "-c",
        BOOTSTRAP,
    ]


def isolated_run(payload: dict[str, Any], image: str = IMAGE) -> dict[str, Any]:
    """Docker is mandatory. Bound output while reading; kill the container, not just its CLI."""
    if shutil.which("docker") is None:
        return {"status": "blocked", "reason": "Docker unavailable; no host fallback"}
    worker = Path(__file__).with_name("live_answer_coding_runner.py").read_text("utf-8")
    name = "career-pilot-coding-" + uuid.uuid4().hex
    try:
        raw_contracts = Path(__file__).with_name("live_answer_coding_contracts.json").read_bytes()
        # Git may convert line endings on Windows; the reviewed UTF-8 content is identical.
        if hashlib.sha256(raw_contracts.replace(b"\r\n", b"\n")).hexdigest() != CONTRACTS_SHA256:
            return {"status": "blocked", "reason": "prospective contract fingerprint mismatch"}
        contracts = json.loads(raw_contracts)
    except (OSError, ValueError):
        return {"status": "blocked", "reason": "prospective contracts unavailable"}
    payload = {**payload, "vectors": contracts["cases"].get(payload.get("case"), [])}
    cmd = docker_command(image, name)
    started = time.monotonic()
    output = bytearray()
    overflow = threading.Event()
    proc: subprocess.Popen[bytes] | None = None

    def execute() -> dict[str, Any]:
        nonlocal proc
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        assert proc.stdout is not None
        assert proc.stdin is not None
        stream = proc.stdout

        def drain() -> None:
            while data := stream.read(4096):
                remaining = MAX_OUTPUT_BYTES - len(output)
                output.extend(data[: max(0, remaining)])
                if len(data) > remaining:
                    overflow.set()
                    return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        pipe = proc.stdin

        def feed() -> None:
            try:
                pipe.write(json.dumps({"worker": worker, "payload": payload}).encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                pipe.close()

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        while proc.poll() is None and not overflow.is_set():
            if time.monotonic() - started >= TIMEOUT_S:
                break
            time.sleep(0.02)
        if overflow.is_set():
            return {"status": "output_limit", "reason": "bounded output exceeded"}
        if proc.poll() is None:
            return {"status": "timeout", "reason": "isolated program exceeded wall limit"}
        reader.join(timeout=1)
        if proc.returncode:
            return {
                "status": "blocked",
                "reason": "container failed or resource limit reached",
                "exit_code": proc.returncode,
                "diagnostic": output.decode(errors="replace")[-1000:],
            }
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            return {"status": "invalid_output", "reason": "program polluted bounded result channel"}
        if not isinstance(result, dict):
            return {"status": "invalid_output", "reason": "invalid result schema"}
        result = cast(dict[str, Any], result)
        if result.get("schema") != 1:
            return {"status": "invalid_output", "reason": "invalid result schema"}
        return result

    outcome: dict[str, Any]
    try:
        outcome = execute()
    except (OSError, subprocess.SubprocessError) as exc:
        outcome = {"status": "blocked", "reason": type(exc).__name__}
    finally:
        # Stop the attached Docker CLI first. After an output-limit event the reader has
        # stopped draining; an attached full pipe can otherwise stall daemon-side removal.
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()
        # Killing the CLI does not stop the container. Explicit removal is still mandatory.
        cleanup_ok = False
        cleanup_detail = ""
        try:
            cleanup = subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True, timeout=5, check=False
            )
            cleanup_detail = cleanup.stderr.decode(errors="replace")[-300:]
            cleanup_ok = cleanup.returncode == 0 or b"No such container" in cleanup.stderr
            if not cleanup_ok:
                # --rm can race explicit removal and report removal already in progress.
                for _ in range(3):
                    inspect_result = subprocess.run(
                        ["docker", "container", "inspect", "--format", "{{.State.Running}}", name],
                        capture_output=True,
                        timeout=3,
                        check=False,
                    )
                    missing = inspect_result.stderr.lower()
                    if inspect_result.returncode and (
                        b"no such container" in missing or b"no such object" in missing
                    ):
                        cleanup_ok = True
                        break
                    time.sleep(0.05)
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_detail = type(exc).__name__
    outcome["container_cleanup_confirmed"] = cleanup_ok
    if not cleanup_ok:
        outcome["cleanup_warning"] = "Container removal could not be confirmed"
        outcome["cleanup_detail"] = cleanup_detail
        outcome["container_name"] = name
    return outcome


def evaluate(entry: dict[str, Any], image: str = IMAGE) -> dict[str, Any]:
    code, errors = selected_code(entry)
    identity = {k: entry.get(k) for k in ("answer_id", "case_id", "answer_sha256", "code_sha256")}
    identity.update(
        image_id=image,
        reviewer=entry.get("reviewer"),
        reviewer_kind=entry.get("reviewer_kind", "unspecified"),
        adapter=entry.get("adapter"),
        generation_completed=entry.get("completed"),
        evaluator_sha256=fingerprint(Path(__file__).read_text("utf-8")),
    )
    identity["contracts_sha256"] = fingerprint(
        Path(__file__).with_name("live_answer_coding_contracts.json").read_text("utf-8")
    )
    identity["worker_sha256"] = fingerprint(
        Path(__file__).with_name("live_answer_coding_runner.py").read_text("utf-8")
    )
    if errors:
        # Approved unchanged source that cannot parse is an observed program defect.
        if errors == ["invalid Python: SyntaxError"]:
            return {
                **identity,
                "status": "failed",
                "failure_kind": "syntax_error",
                "reasons": errors,
            }
        return {**identity, "status": "manual_review_required", "reasons": errors}
    result = isolated_run(
        {"case": CASES[entry["case_id"]], "code": code, "adapter": entry["adapter"]}, image
    )
    return {**identity, **result}


def evaluate_review(entries: list[dict[str, Any]], image: str = IMAGE) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    blocked = False
    for entry in entries:
        if blocked:
            results.append(
                {
                    "answer_id": entry.get("answer_id"),
                    "status": "not_dispatched",
                    "reason": "previous sandbox infrastructure/cleanup failure",
                }
            )
            continue
        result = evaluate(entry, image)
        results.append(result)
        blocked = (
            result.get("container_cleanup_confirmed") is False or result["status"] == "blocked"
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--trials", type=Path, required=True)
    prepare_parser.add_argument("--out", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--review", type=Path, required=True)
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output already exists; preserve previous evaluation artifacts")
    result: dict[str, Any]
    if args.command == "prepare":
        source = args.trials.read_text("utf-8")
        result = prepare(json.loads(source))
        result["source_trials_sha256"] = fingerprint(source)
    else:
        review = json.loads(args.review.read_text("utf-8"))
        if review.get("schema") != 1:
            parser.error("Unsupported review schema")
        result = {
            "schema": 1,
            "source_trials_sha256": review.get("source_trials_sha256"),
            "review_sha256": fingerprint(args.review.read_text("utf-8")),
            "results": evaluate_review(review["entries"], args.image),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "output": str(args.out),
                "entries": len(result.get("entries", result.get("results", []))),
            }
        )
    )


if __name__ == "__main__":
    main()
