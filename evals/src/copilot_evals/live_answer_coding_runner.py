"""Trusted deterministic test runner, copied into the isolated container without host mounts.

Never pass generated code to this module on the host. Its main entry point requires a container
marker set by the host evaluator, and the host always uses a pinned, networkless Docker image.
"""

from __future__ import annotations

import asyncio
import collections
import gc
import json
import os
import random
import re
import sys
import time
import types
from collections.abc import Callable, Iterable
from typing import Any, cast


class ContractError(Exception):
    pass


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
        raise ContractError("Explicit public entrypoint/method/argument name required")
    return value


def invoke(fn: Callable[..., Any], spec: Any, values: dict[str, Any]) -> Any:
    if not isinstance(spec, dict):
        raise ContractError("Call adapter must be an object")
    spec = cast(dict[str, Any], spec)
    if set(spec) - {"name", "args", "kwargs"}:
        raise ContractError("Call adapter accepts name, args and kwargs only")
    args, kwargs = spec.get("args", []), spec.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ContractError("Call args/kwargs must be declarative token mappings")
    args = cast(list[Any], args)
    kwargs = cast(dict[str, Any], kwargs)
    tokens = args + list(kwargs.values())
    if any(not isinstance(token, str) or token not in values for token in tokens):
        raise ContractError("Unknown adapter argument token")
    return fn(
        *(values[token] for token in args),
        **{identifier(key): values[token] for key, token in kwargs.items()},
    )


def method(obj: Any, spec: Any, values: dict[str, Any]) -> Any:
    if not isinstance(spec, dict):
        raise ContractError("Method adapter required")
    spec = cast(dict[str, Any], spec)
    return invoke(getattr(obj, identifier(spec.get("name"))), spec, values)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.measurements: dict[str, Any] = {}

    def check(self, name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            self.results.append({"name": name, "passed": True})
        except ContractError:
            raise
        except BaseException as exc:
            self.results.append(
                {"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"[:400]}
            )

    def equal(self, name: str, got: Any, expected: Any) -> None:
        self.check(name, lambda: require(got == expected, f"expected {expected!r}; got {got!r}"))


def first_unique(fn: Callable[..., Any], adapter: dict[str, Any], checks: Checks) -> None:
    kind = adapter.get("result")
    if kind not in ("character", "index") or "missing" not in adapter:
        raise ContractError("Declare result character/index and the no-result sentinel")
    samples = ["", "a", "aabb", "swiss", "aabbc", "aabcbcde", "AaA", "éé🙂b🙂", " \t "]
    rng = random.Random(173)
    samples += ["".join(rng.choice("abcé🙂") for _ in range(rng.randrange(40))) for _ in range(80)]
    for i, text in enumerate(samples):
        index = next((n for n, char in enumerate(text) if text.count(char) == 1), None)
        want = (
            adapter["missing"] if index is None else text[index] if kind == "character" else index
        )
        checks.check(
            f"first_unique_{i}",
            lambda text=text, want=want: require(
                fn(text) == want, f"wrong first unique for {text!r}"
            ),
        )


def intervals(fn: Callable[..., Any], checks: Checks) -> None:
    def reference(values: list[list[int]]) -> list[list[int]]:
        # Independent connected-components oracle: edges exist only for strict overlap.
        if any(a > b for a, b in values):
            raise ValueError("reversed")
        groups: list[list[int]] = []
        for a, b in values:
            if a == b:
                continue
            overlaps = [g for g in groups if a < g[1] and g[0] < b]
            while overlaps:
                for group in overlaps:
                    a, b = min(a, group[0]), max(b, group[1])
                    groups.remove(group)
                overlaps = [g for g in groups if a < g[1] and g[0] < b]
            groups.append([a, b])
        return sorted(groups)

    examples = [
        [],
        [[1, 1]],
        [[1, 2], [2, 3]],
        [[3, 5], [1, 4]],
        [[-4, 0], [-3, -1], [0, 2]],
        [[1, 4], [1, 4], [2, 3]],
        [[0, 2], [2, 4], [1, 3]],
        [[1, 2], [3, 3], [4, 6]],
    ]
    rng = random.Random(991)
    examples += [
        [sorted([rng.randrange(-10, 11), rng.randrange(-10, 11)]) for _ in range(rng.randrange(15))]
        for _ in range(80)
    ]
    for i, original in enumerate(examples):

        def one(original: list[list[int]] = original) -> None:
            values = [pair[:] for pair in original]
            result = [list(pair) for pair in fn(values)]
            require(values == original, "input mutated")
            require(result == reference(original), f"wrong merged intervals: {result!r}")
            require(
                [list(pair) for pair in fn([p[:] for p in result])] == result,
                "merge is not idempotent",
            )

        checks.check(f"intervals_{i}", one)
    for i, values in enumerate(([[2, 1]], [[0, 3], [9, 8]], [[1, 1], [0, -1]])):

        def invalid(values: list[list[int]] = values) -> None:
            try:
                fn(values)
            except ValueError:
                return
            raise AssertionError("reversed interval must raise ValueError")

        checks.check(f"reversed_{i}", invalid)


def retained_slots(obj: Any) -> int:
    """Measure reachable data containers, not classes/modules/closures or allocator guesses."""
    seen: set[int] = set()
    stack = [obj]
    count = 0
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if len(seen) > 100_000:
            raise AssertionError("retained state graph exceeds bounded observation limit")
        if isinstance(current, dict):
            current = cast(dict[Any, Any], current)
            count += len(current)
            stack.extend(current.values())
        elif isinstance(current, (list, tuple, set, collections.deque)):
            values = cast(list[Any], current)
            count += len(values)
            stack.extend(values)
        elif type(current).__module__ == "candidate":
            if hasattr(current, "__dict__"):
                stack.append(vars(current))
            else:
                raise ContractError("Slots-only state needs explicit manual memory review")
    return count


def growth_probe(obj: Any, update: Callable[[], Any], checks: Checks) -> None:
    sizes: list[int] = []
    done = 0
    for target in (128, 1024, 8192):
        for _ in range(target - done):
            update()
        done = target
        gc.collect()
        sizes.append(retained_slots(obj))
    checks.measurements["retained_container_slots_after_updates"] = dict(
        zip(("128", "1024", "8192"), sizes, strict=True)
    )
    require(
        not (sizes[2] > max(128, sizes[0] * 8) and sizes[2] > sizes[1] * 4),
        f"obsolete-record growth witnessed at fixed live-key count: {sizes}",
    )


def expiring_lru(factory: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    missing = a.get("missing")
    if not isinstance(missing, dict):
        raise ContractError("Declare missing kind value (+value) or KeyError")
    missing = cast(dict[str, Any], missing)
    if missing.get("kind") not in ("value", "KeyError"):
        raise ContractError("Declare missing kind value (+value) or KeyError")
    if missing["kind"] == "value" and "value" not in missing:
        raise ContractError("Declare the cache miss value")

    def scenario(capacity: int = 2) -> tuple[Any, Clock]:
        clock = Clock()
        obj = invoke(factory, a.get("construct"), {"capacity": capacity, "clock": clock})
        return obj, clock

    def put(obj: Any, key: str, value: str, ttl: int) -> Any:
        return method(obj, a.get("put"), {"key": key, "value": value, "ttl": ttl})

    def get(obj: Any, key: str) -> Any:
        return method(obj, a.get("get"), {"key": key})

    def miss(obj: Any, key: str) -> None:
        if missing["kind"] == "KeyError":
            try:
                get(obj, key)
            except KeyError:
                return
            raise AssertionError("expected cache miss KeyError")
        require(get(obj, key) == missing["value"], "expected declared cache miss")

    def expiry() -> None:
        obj, clock = scenario()
        put(obj, "a", "value-a", 10)
        clock.now = 9.999
        require(get(obj, "a") == "value-a", "entry expired too early")
        clock.now = 10
        miss(obj, "a")

    c.check("ttl_exact_expiry", expiry)

    def renewal() -> None:
        obj, clock = scenario()
        put(obj, "a", "old", 10)
        clock.now = 9
        put(obj, "a", "new", 20)
        clock.now = 10
        require(get(obj, "a") == "new", "old expiry invalidated replacement")
        clock.now = 28.999
        require(get(obj, "a") == "new", "TTL did not renew")
        clock.now = 29
        miss(obj, "a")

    c.check("replacement_renews_ttl", renewal)

    def shortened() -> None:
        obj, clock = scenario()
        put(obj, "a", "old", 100)
        clock.now = 1
        put(obj, "a", "new", 2)
        clock.now = 3
        miss(obj, "a")
        put(obj, "zero", "zero", 0)
        miss(obj, "zero")

    c.check("replacement_shortens_ttl_and_zero_ttl", shortened)

    def lru() -> None:
        obj, _ = scenario()
        put(obj, "a", "a", 100)
        put(obj, "b", "b", 100)
        require(get(obj, "a") == "a", "live key missing")
        put(obj, "c", "c", 100)
        miss(obj, "b")
        require(get(obj, "a") == "a" and get(obj, "c") == "c", "wrong LRU eviction")

    c.check("access_updates_recency", lru)

    def expired_capacity() -> None:
        obj, clock = scenario()
        put(obj, "a", "a", 100)
        put(obj, "b", "b", 1)
        clock.now = 2
        put(obj, "c", "c", 100)
        require(get(obj, "a") == "a", "expired key caused live LRU eviction")
        miss(obj, "b")

    c.check("expiry_releases_capacity", expired_capacity)

    def zero() -> None:
        obj, _ = scenario(0)
        put(obj, "a", "a", 10)
        miss(obj, "a")

    c.check("zero_capacity", zero)

    def memory() -> None:
        obj, _ = scenario()
        growth_probe(obj, lambda: put(obj, "a", "a", 60), c)

    c.check("obsolete_expiry_record_growth", memory)


def dedup_window(factory: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    if not {"new_result", "duplicate_result", "expires_at_boundary"} <= a.keys():
        raise ContractError("Declare new/duplicate outputs and exact boundary convention")
    if type(a["expires_at_boundary"]) is not bool or a.get("boundary_policy_stated") is not True:
        raise ContractError("Boundary policy must be explicitly stated in the original answer")
    if a["new_result"] == a["duplicate_result"]:
        raise ContractError("New and duplicate results must differ")

    def scenario() -> tuple[Any, Clock]:
        clock = Clock()
        return invoke(factory, a.get("construct"), {"clock": clock, "window": 60}), clock

    def observe(obj: Any, key: str) -> Any:
        return method(obj, a.get("observe"), {"key": key})

    def basic() -> None:
        obj, clock = scenario()
        require(observe(obj, "a") == a["new_result"], "first event not new")
        clock.now = 59
        require(observe(obj, "a") == a["duplicate_result"], "retry not suppressed")
        clock.now = 61
        require(observe(obj, "a") == a["duplicate_result"], "retry did not extend retention")
        require(observe(obj, "b") == a["new_result"], "independent ID suppressed")
        clock.now = 121.001
        require(observe(obj, "a") == a["new_result"], "ID retained past renewed window")

    c.check("retry_extends_retention_and_independent_ids", basic)

    def boundary() -> None:
        obj, clock = scenario()
        observe(obj, "a")
        clock.now = 60
        want = a["new_result"] if a["expires_at_boundary"] else a["duplicate_result"]
        require(observe(obj, "a") == want, "exact boundary differs from declared policy")

    c.check("declared_exact_boundary", boundary)

    def expired_keys() -> None:
        obj, clock = scenario()
        for i in range(512):
            observe(obj, str(i))
        before = retained_slots(obj)
        clock.now = 61
        observe(obj, "new-window")
        after = retained_slots(obj)
        c.measurements["expired_key_retained_slots"] = {"before": before, "after": after}
        require(after < before // 2, "expired IDs accumulated across processing-time windows")

    c.check("expired_ids_leave_retention_window", expired_keys)

    def memory() -> None:
        obj, _ = scenario()
        growth_probe(obj, lambda: observe(obj, "a"), c)

    c.check("obsolete_expiry_record_growth", memory)


def bounded_workers(fn: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    async def bounded() -> None:
        total, consumed, active, peak, spawned = 2000, 0, 0, 0, 0
        gate = asyncio.Event()
        first = asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()

        def factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
            nonlocal spawned
            spawned += 1
            return asyncio.Task(coro, loop=loop, **kwargs)

        def items() -> Iterable[int]:
            nonlocal consumed
            for i in range(total):
                consumed += 1
                yield i

        async def operation(i: int) -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            first.set()
            try:
                await gate.wait()
                await asyncio.sleep(0)
                return i * 2
            finally:
                active -= 1

        loop.set_task_factory(factory)
        task = asyncio.create_task(
            invoke(fn, a.get("call"), {"operation": operation, "items": items(), "limit": 8})
        )
        try:
            await asyncio.wait_for(first.wait(), 1)
            for _ in range(5):
                await asyncio.sleep(0)
            c.measurements.update(initial_consumed=consumed, initial_tasks_created=spawned)
            require(consumed < total, "entire iterable consumed before any work completed")
            require(spawned < total // 2, "tasks created for the input up front")
            require(peak <= 8, f"{peak} operations in flight")
            gate.set()
            result = await asyncio.wait_for(task, 3)
            require(list(result) == [i * 2 for i in range(total)], "result order/value mismatch")
            require(peak <= 8 and active == 0, "concurrency limit or cleanup failed")
            c.measurements["peak_operations"] = peak
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            loop.set_task_factory(previous)

    async def ordered() -> None:
        async def operation(i: int) -> int:
            await asyncio.sleep((7 - i % 8) * 0.001)
            return i

        result = await invoke(
            fn, a.get("call"), {"operation": operation, "items": range(32), "limit": 8}
        )
        require(list(result) == list(range(32)), "completion order leaked into result order")

    async def empty() -> None:
        async def operation(value: Any) -> Any:
            raise AssertionError("operation invoked for empty input")

        result = await invoke(
            fn, a.get("call"), {"operation": operation, "items": iter(()), "limit": 8}
        )
        require(list(result) == [], "empty input did not produce empty results")

    async def failure() -> None:
        error = ValueError("deterministic first failure")
        active: set[int] = set()
        finished: set[int] = set()
        gate = asyncio.Event()

        async def operation(i: int) -> int:
            active.add(i)
            try:
                if i == 0:
                    await asyncio.sleep(0)
                    raise error
                await gate.wait()
                return i
            finally:
                await asyncio.sleep(0)
                finished.add(i)

        try:
            await asyncio.wait_for(
                invoke(fn, a.get("call"), {"operation": operation, "items": range(64), "limit": 8}),
                1,
            )
        except (ValueError, ExceptionGroup) as exc:
            leaves = list(exc.exceptions) if isinstance(exc, ExceptionGroup) else [exc]
            require(leaves == [error], "first error identity not preserved")
            require(active == finished, "remaining tasks not cancelled and awaited")
        else:
            raise AssertionError("first error did not propagate")
        finally:
            gate.set()

    for name, body in (
        ("bounded_lazy_work", bounded),
        ("ordered_results", ordered),
        ("empty_input", empty),
        ("first_error_cancels_and_awaits", failure),
    ):
        c.check(name, lambda body=body: asyncio.run(body()))


def utf8_stream(fn: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    policy = a.get("invalid_bytes")
    if policy not in ("strict", "replace", "ignore") or a.get("invalid_policy_stated") is not True:
        raise ContractError("Declare the original answer's explicit invalid-byte policy")

    def run(chunks: Iterable[bytes]) -> Any:
        return invoke(fn, a.get("call"), {"chunks": chunks, "invalid_bytes": policy})

    def expected(data: bytes) -> list[str]:
        text = data.decode("utf-8", errors=policy)
        parts = text.split("\n")
        complete = [part[:-1] if part.endswith("\r") else part for part in parts[:-1]]
        if parts[-1]:
            complete.append(parts[-1])
        return complete

    texts = ["", "a\n", "\n\n", "  é🙂  \r\n東京\nlast", "é\r\n🙂\r\n", "tail\r"]
    for text in texts:
        data = text.encode("utf-8")
        for split in range(len(data) + 1):
            chunks = [data[:split], b"", data[split:]]
            c.check(
                f"utf8_split_{len(c.results)}",
                lambda chunks=chunks, data=data: require(
                    list(run(chunks)) == expected(data), "record/chunk boundary mismatch"
                ),
            )
        c.check(
            f"utf8_bytewise_{len(c.results)}",
            lambda data=data: require(
                list(run(bytes([byte]) for byte in data)) == expected(data),
                "bytewise chunks corrupted records",
            ),
        )

    def progressive() -> None:
        consumed = 0

        def chunks() -> Iterable[bytes]:
            nonlocal consumed
            for chunk in (b"first\n", b"trailing"):
                consumed += 1
                yield chunk

        output = iter(run(chunks()))
        require(next(output) == "first", "complete record missing")
        require(int(consumed) == 1, "complete record delayed while reading additional chunks")
        require(list(output) == ["trailing"], "trailing record not preserved until EOF")

    c.check("progressive_records_and_final_flush", progressive)
    for i, data in enumerate((b"a\xffb\n", b"\xe2\x82", b"\xc0\xaf\n", b"a\xe2(\xa1z")):

        def invalid(data: bytes = data) -> None:
            chunks = [bytes([byte]) for byte in data]
            if policy == "strict":
                try:
                    list(run(chunks))
                except UnicodeDecodeError:
                    return
                raise AssertionError("strict policy accepted invalid UTF-8")
            require(list(run(chunks)) == expected(data), "declared invalid-byte policy mismatch")

        c.check(f"invalid_utf8_{i}", invalid)


def isolated_worker() -> bool:
    return (
        os.environ.get("CAREER_PILOT_CODING_ISOLATED") == "1"
        and os.path.exists("/.dockerenv")
        and getattr(os, "geteuid", lambda: -1)() == 65534
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isolated_worker():
        return {"schema": 1, "status": "blocked", "reason": "isolated worker required"}
    a = payload.get("adapter")
    if not isinstance(a, dict):
        return {"schema": 1, "status": "manual_review_required", "reason": "adapter required"}
    a = cast(dict[str, Any], a)
    checks = Checks()
    started = time.monotonic()
    try:
        name = identifier(a.get("entrypoint"))
        module = types.ModuleType("candidate")
        sys.modules["candidate"] = module
        namespace: dict[str, Any] = vars(module)
        exec(compile(payload["code"], "<unchanged-answer>", "exec"), namespace)
        fn = namespace.get(name)
        if not callable(fn):
            raise ContractError("Reviewed entrypoint is not callable")
        case = payload["case"]
        if case == "first_unique":
            first_unique(fn, a, checks)
        elif case == "intervals":
            intervals(fn, checks)
        elif case == "expiring_lru":
            expiring_lru(fn, a, checks)
        elif case == "dedup_window":
            dedup_window(fn, a, checks)
        elif case == "bounded_workers":
            bounded_workers(fn, a, checks)
        elif case == "utf8_stream":
            utf8_stream(fn, a, checks)
        else:
            raise ContractError("Unsupported case")
    except ContractError as exc:
        return {
            "schema": 1,
            "status": "manual_review_required",
            "reason": str(exc),
            "checks": checks.results,
            "measurements": checks.measurements,
        }
    except BaseException as exc:
        return {"schema": 1, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:400]}
    return {
        "schema": 1,
        "status": "passed" if all(x["passed"] for x in checks.results) else "failed",
        "checks": checks.results,
        "measurements": checks.measurements,
        "elapsed_s": time.monotonic() - started,
        "limitations": [
            "Finite deterministic cases do not prove asymptotic complexity.",
            "Container timing is not model or live-answer latency.",
            "Memory growth is an observed witness, not a general boundedness proof.",
        ],
    }


if __name__ == "__main__":
    if not isolated_worker():
        raise SystemExit("Refusing generated-code execution outside the isolated container")
    payload = json.loads(sys.stdin.read())
    print(json.dumps(run(payload)))
