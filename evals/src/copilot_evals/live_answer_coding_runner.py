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

    def unscored(self, name: str, reason: str, scope: str = "declared_policy") -> None:
        self.results.append(
            {"name": name, "scope": scope, "status": "unscored", "passed": None, "detail": reason}
        )

    def check(self, name: str, fn: Callable[[], Any], scope: str = "mandatory") -> None:
        try:
            fn()
            self.results.append({"name": name, "scope": scope, "status": "passed", "passed": True})
        except ContractError as exc:
            self.unscored(name, str(exc), scope)
        except BaseException as exc:
            self.results.append(
                {
                    "name": name,
                    "scope": scope,
                    "status": "failed",
                    "passed": False,
                    "detail": f"{type(exc).__name__}: {exc}"[:400],
                }
            )

    def equal(self, name: str, got: Any, expected: Any) -> None:
        self.check(name, lambda: require(got == expected, f"expected {expected!r}; got {got!r}"))

    def summary(self) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {}
        for item in self.results:
            group = counts.setdefault(item["scope"], {"passed": 0, "failed": 0, "unscored": 0})
            group[item["status"]] += 1
        functional = [
            row for row in self.results if row["scope"] not in ("diagnostic", "memory_witness")
        ]
        memory_review = any(
            row["scope"] == "memory_witness" and row["passed"] is not True for row in self.results
        )
        failed = any(row["passed"] is False for row in functional)
        passed = any(row["passed"] is True for row in functional)
        unknown = any(row["passed"] is None for row in functional) or memory_review
        status = (
            "failed"
            if failed
            else "partially_scored"
            if passed and unknown
            else "passed"
            if passed
            else "manual_review_required"
        )
        return {
            "status": status,
            "functional_status": "failed"
            if failed
            else "unscored"
            if not passed
            else "partially_scored"
            if any(row["passed"] is None for row in functional)
            else "passed",
            "memory_review_required": memory_review,
            "scope_counts": counts,
            "checks": self.results,
            "measurements": self.measurements,
        }


def first_unique(fn: Callable[..., Any], adapter: dict[str, Any], checks: Checks) -> None:
    kind = adapter.get("result")
    if kind not in ("character", "index") or "missing" not in adapter:
        raise ContractError("Declare result character/index and no-result sentinel")
    samples = ["", "a", "aabb", "swiss", "aabbc", "aabcbcde", "AaA", " \t "]
    rng = random.Random(173)
    samples += ["".join(rng.choice("abc") for _ in range(rng.randrange(40))) for _ in range(40)]
    if adapter.get("character_unit") == "code_points":
        samples += ["éé🙂b🙂", "e\u0301e\u0301z"]
    else:
        checks.unscored(
            "unicode_character_semantics",
            "Unicode unit is undeclared or unsupported; ASCII subset only",
        )
    for i, text in enumerate(samples):
        index = next((n for n, char in enumerate(text) if text.count(char) == 1), None)
        want = (
            adapter["missing"] if index is None else text[index] if kind == "character" else index
        )
        checks.check(
            f"first_unique_{i}",
            lambda text=text, want=want: require(fn(text) == want, "wrong earliest unique element"),
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
            result = sorted(list(pair) for pair in fn(values))
            require(values == original, "input mutated")
            require(result == reference(original), f"wrong merged intervals: {result!r}")
            require(
                sorted(list(pair) for pair in fn([p[:] for p in result])) == result,
                "merge is not idempotent",
            )

        checks.check(f"intervals_{i}", one)
    for i, values in enumerate(([[2, 1]], [[0, 3], [9, 8]], [[1, 1], [0, -1]])):

        def invalid(values: list[list[int]] = values) -> None:
            try:
                list(fn(values))
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
        clock.now = 10.001
        miss(obj, "a")

    c.check("ttl_before_after_expiry", expiry)

    def renewal() -> None:
        obj, clock = scenario()
        put(obj, "a", "old", 10)
        clock.now = 9
        put(obj, "a", "new", 20)
        clock.now = 10
        require(get(obj, "a") == "new", "old expiry invalidated replacement")
        clock.now = 28.999
        require(get(obj, "a") == "new", "TTL did not renew")
        clock.now = 29.001
        miss(obj, "a")

    c.check("replacement_renews_ttl", renewal)

    def shortened() -> None:
        obj, clock = scenario()
        put(obj, "a", "old", 100)
        clock.now = 1
        put(obj, "a", "new", 2)
        clock.now = 3.001
        miss(obj, "a")

    c.check("replacement_shortens_ttl", shortened)

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

    c.check("obsolete_expiry_record_growth", memory, "memory_witness")

    def equality() -> None:
        boundary = a.get("expires_at_boundary")
        if type(boundary) is not bool or a.get("boundary_policy_reviewed") is not True:
            raise ContractError(
                "Exact-expiry policy is undeclared; strict-before/after checks still apply"
            )
        obj, clock = scenario()
        put(obj, "a", "value-a", 10)
        clock.now = 10
        if boundary:
            miss(obj, "a")
        else:
            require(get(obj, "a") == "value-a", "declared equality policy mismatch")

    c.check("declared_expiry_equality", equality, "declared_policy")


def dedup_window(factory: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    if not {"new_result", "duplicate_result"} <= a.keys():
        raise ContractError("Declare new/duplicate outputs and exact boundary convention")
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
        if (
            type(a.get("expires_at_boundary")) is not bool
            or a.get("boundary_policy_stated") is not True
        ):
            raise ContractError("Boundary policy must be explicitly stated in original answer")
        obj, clock = scenario()
        observe(obj, "a")
        clock.now = 60
        want = a["new_result"] if a["expires_at_boundary"] else a["duplicate_result"]
        require(observe(obj, "a") == want, "exact boundary differs from declared policy")

    c.check("declared_exact_boundary", boundary, "declared_policy")

    def expired_keys() -> None:
        obj, clock = scenario()
        for i in range(512):
            observe(obj, str(i))
        before = retained_slots(obj)
        clock.now = 61
        observe(obj, "new-window")
        after = retained_slots(obj)
        c.measurements["expired_key_retained_slots"] = {"before": before, "after": after}
        require(after >= 0, "invalid state observation")

    c.check("expired_ids_leave_retention_window", expired_keys, "diagnostic")

    def memory() -> None:
        obj, _ = scenario()
        growth_probe(obj, lambda: observe(obj, "a"), c)

    c.check("obsolete_expiry_record_growth", memory, "memory_witness")

    def expired_growth() -> None:
        obj, clock = scenario()

        def update() -> None:
            clock.now += 61
            observe(obj, str(clock.now))

        growth_probe(obj, update, c)

    c.check("expired_distinct_key_growth", expired_growth, "memory_witness")


def bounded_workers(fn: Callable[..., Any], a: dict[str, Any], c: Checks) -> None:
    async def collect_call(values: dict[str, Any]) -> list[Any]:
        if a.get("result") not in ("awaitable", "async_iterator"):
            raise ContractError("Declare awaitable or async_iterator result")
        result = invoke(fn, a.get("call"), values)
        if a["result"] == "awaitable":
            return list(await result)
        return [value async for value in result]

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
            collect_call({"operation": operation, "items": items(), "limit": 8})
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

        result = await collect_call({"operation": operation, "items": range(32), "limit": 8})
        require(list(result) == list(range(32)), "completion order leaked into result order")

    async def empty() -> None:
        async def operation(value: Any) -> Any:
            raise AssertionError("operation invoked for empty input")

        result = await collect_call({"operation": operation, "items": iter(()), "limit": 8})
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
                collect_call({"operation": operation, "items": range(64), "limit": 8}),
                1,
            )
        except (ValueError, ExceptionGroup) as exc:
            leaves: list[Exception] = []
            pending: list[Exception] = [exc]
            while pending:
                current = pending.pop()
                if isinstance(current, ExceptionGroup):
                    pending.extend(cast(ExceptionGroup[Exception], current).exceptions)
                else:
                    leaves.append(current)
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
    policy_known = (
        policy in ("strict", "replace", "ignore") and a.get("invalid_policy_stated") is True
    )
    delimiter = a.get("delimiter_mode")
    if delimiter not in ("stripped", "preserved") or a.get("delimiter_policy_reviewed") is not True:
        raise ContractError(
            "Declare original delimiter representation; do not infer from test results"
        )

    def run(chunks: Iterable[bytes]) -> Any:
        return invoke(
            fn,
            a.get("call"),
            {"chunks": chunks, **({"invalid_bytes": policy} if policy_known else {})},
        )

    def expected(data: bytes) -> list[str]:
        text = data.decode("utf-8", errors=cast(str, policy) if policy_known else "strict")
        parts = text.split("\n")
        complete = (
            [part + "\n" for part in parts[:-1]]
            if delimiter == "preserved"
            else [part[:-1] if part.endswith("\r") else part for part in parts[:-1]]
        )
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
        require(
            next(output) == ("first\n" if delimiter == "preserved" else "first"),
            "complete record missing",
        )
        require(int(consumed) == 1, "complete record delayed while reading additional chunks")
        require(list(output) == ["trailing"], "trailing record not preserved until EOF")

    c.check("progressive_records_and_final_flush", progressive)
    if not policy_known:
        c.unscored(
            "invalid_byte_policy", "Original response did not state a supported invalid-byte policy"
        )
        return
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

        c.check(f"invalid_utf8_{i}", invalid, "declared_policy")


def declared_error(body: Callable[[], Any], spec: Any, *, exception_only: bool = False) -> None:
    if not isinstance(spec, dict):
        raise ContractError("Declare rejection as exception types or a specific return value")
    spec = cast(dict[str, Any], spec)
    if spec.get("kind") not in ("exception", "value"):
        raise ContractError("Declare rejection as exception types or a specific return value")
    if exception_only and spec["kind"] != "exception":
        raise ContractError("This question explicitly requires raising an error")
    if spec["kind"] == "value":
        if "value" not in spec:
            raise ContractError("Declare rejection sentinel")
        require(body() == spec["value"], "invalid input not rejected with declared sentinel")
        return
    names = spec.get("types")
    if (
        not isinstance(names, list)
        or not names
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", x)
            for x in cast(list[Any], names)
        )
    ):
        raise ContractError(
            "Declare explicit exception type names; never evaluate arbitrary adapter code"
        )
    try:
        body()
    except Exception as exc:
        require(
            any(base.__name__ in names for base in type(exc).__mro__), "wrong rejection exception"
        )
        return
    raise AssertionError("invalid input accepted")


def escaped_path(
    fn: Callable[..., Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    for item in vectors:
        text, expected = item["input"]["path"], item["expected"]

        def one(text: str = text, expected: Any = expected) -> None:
            def call() -> Any:
                return invoke(fn, a.get("call"), {"path": text})

            if isinstance(expected, dict):

                def rejected_call() -> Any:
                    result = call()
                    spec = a.get("rejection")
                    if (
                        isinstance(spec, dict)
                        and cast(dict[str, Any], spec).get("kind") == "value"
                        and result == cast(dict[str, Any], spec).get("value")
                    ):
                        return result
                    return list(result)

                declared_error(rejected_call, a.get("rejection"))
            else:
                require(list(call()) == expected, "escape or component boundary mismatch")

        c.check(item["id"], one)


def lazy_merge(
    fn: Callable[..., Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    shape = a.get("call_shape")
    if shape not in ("streams", "variadic"):
        raise ContractError("Declare streams or variadic call shape")

    def call(streams: list[Any]) -> Any:
        return (
            fn(*streams) if shape == "variadic" else invoke(fn, a.get("call"), {"streams": streams})
        )

    for item in vectors:
        if item["scope"] != "mandatory":
            continue

        def one(item: dict[str, Any] = item) -> None:
            result = call([iter(row) for row in item["input"]["streams"]])
            require(
                iter(result) is result, "result must be an iterator, not a precomputed collection"
            )
            require(list(result) == item["expected"], "sorted merge lost/reordered values")

        c.check(item["id"], one)

    def progressive() -> None:
        consumed = [0, 0, 0]
        emitted = [0, 0, 0]

        def source(index: int) -> Iterable[int]:
            n = 0
            while True:
                consumed[index] += 1
                require(consumed[index] - emitted[index] <= 2, "unbounded prefetch before output")
                yield n * 3 + index
                n += 1

        output = call([iter(source(i)) for i in range(3)])
        require(iter(output) is output, "result is not an iterator")
        try:
            for wanted in range(20):
                got = next(output)
                require(got == wanted, "wrong infinite-prefix result")
                emitted[wanted % 3] += 1
                require(
                    all(x - y <= 1 for x, y in zip(consumed, emitted, strict=True)),
                    "more than one pending value per input",
                )
        finally:
            close = getattr(output, "close", None)
            if close is not None:
                close()
        c.measurements["merge_consumed_after_20"] = consumed

    c.check("bounded_infinite_prefix", progressive)


def run_length(
    namespace: dict[str, Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    encode = namespace.get(identifier(a.get("entrypoint")))
    decode = namespace.get(identifier(a.get("decode_entrypoint")))
    if not callable(encode) or not callable(decode):
        raise ContractError("Declare original encode and decode entrypoints")
    unit = a.get("character_unit")
    if unit != "code_points":
        raise ContractError(
            "Unicode unit undeclared or unsupported; do not guess a character model"
        )

    def enc(text: str) -> Any:
        return invoke(encode, a.get("encode_call"), {"text": text})

    def dec(records: Any) -> Any:
        return invoke(decode, a.get("decode_call"), {"records": records})

    for item in vectors:
        scope = item["scope"]
        if scope == "declared_policy":
            c.unscored(item["id"], "Bool count convention not a primary requirement", "diagnostic")
            continue

        def one(item: dict[str, Any] = item) -> None:
            values, expected = item["input"], item["expected"]
            if "text" in values:
                records = list(enc(values["text"]))
                for record in records:
                    require(
                        isinstance(record, (list, tuple))
                        and len(cast(list[Any] | tuple[Any, ...], record)) == 2,
                        "not character/count pairs",
                    )
                    char, count = cast(list[Any] | tuple[Any, ...], record)
                    require(isinstance(char, str) and len(char) == 1, "not a code point")
                    require(isinstance(count, int) and count > 0, "not a positive integral count")
                require(dec(records) == values["text"], "Unicode roundtrip changed content")
            elif isinstance(expected, dict):
                declared_error(
                    lambda: dec([list(pair) for pair in values["records"]]), a.get("rejection")
                )
            else:
                require(
                    dec([list(pair) for pair in values["records"]]) == expected,
                    "wrong decoded repetition",
                )

        c.check(item["id"], one)


def dependency_order(
    fn: Callable[..., Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    orientation = a.get("orientation")
    if (
        orientation not in ("successors", "prerequisites", "edges")
        or a.get("orientation_reviewed") is not True
    ):
        raise ContractError("Declare original graph representation before tests")
    for item in vectors:

        def one(item: dict[str, Any] = item) -> None:
            nodes = cast(list[str], item["input"]["nodes"][:])
            edges = cast(
                list[list[str]],
                [pair[:] for pair in item["input"]["edges_prerequisite_to_dependent"]],
            )
            want = item["expected"]
            for reverse in (False, True):
                order = list(reversed(nodes)) if reverse else nodes
                edge_order = list(reversed(edges)) if reverse else edges
                graph: dict[str, list[str]] = {node: [] for node in order}
                for pre, dep in edge_order:
                    graph[dep if orientation == "prerequisites" else pre].append(
                        pre if orientation == "prerequisites" else dep
                    )

                def call(
                    order: list[str] = order,
                    edge_order: list[list[str]] = edge_order,
                    graph: dict[str, list[str]] = graph,
                ) -> Any:
                    return invoke(
                        fn, a.get("call"), {"nodes": order, "edges": edge_order, "graph": graph}
                    )

                if isinstance(want, dict):
                    declared_error(lambda: list(call()), a.get("cycle_error"), exception_only=True)
                else:
                    require(
                        list(call()) == want,
                        "wrong dependency/ready-node order or missing isolated node",
                    )

        c.check(item["id"], one, "declared_policy")


def dependency_invalidation(
    fn: Callable[..., Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    root_policy = a.get("root_policy")
    for item in vectors:
        values = item["input"]
        root = values["changed"]

        # Input stays a reverse map. Never transpose it to repair a wrong-direction program.
        def call(values: dict[str, Any] = values) -> list[Any]:
            mapping = {key: list(value) for key, value in values["reverse_dependencies"].items()}
            return list(invoke(fn, a.get("call"), {"root": values["changed"], "reverse": mapping}))

        def downstream(
            call: Callable[[], list[Any]] = call, item: dict[str, Any] = item, root: str = root
        ) -> None:
            result = call()
            require(result == sorted(set(result)), "not sorted unique output")
            require(
                [node for node in result if node != root]
                == item["expected"]["downstream_excluding_root"],
                "wrong downstream reachability",
            )

        c.check(item["id"] + ":downstream", downstream)
        if (
            root_policy not in ("include_root", "exclude_root")
            or a.get("root_policy_reviewed") is not True
        ):
            c.unscored(
                item["id"] + ":root",
                "Root membership was not declared; "
                "downstream-only score is not whole-program correctness",
            )
        else:
            c.check(
                item["id"] + ":root",
                lambda call=call, item=item: require(
                    call() == item["expected"][root_policy], "root policy mismatch"
                ),
                "declared_policy",
            )


def observed_state_slots(obj: Any) -> int:
    if isinstance(obj, types.GeneratorType):
        frame = obj.gi_frame
        if frame is None:
            raise ContractError("Generator exhausted before state observation")
        # Only local containers are measured; globals/closures are not proved bounded.
        return retained_slots(dict(frame.f_locals))
    if type(obj).__module__ != "candidate":
        raise ContractError("Opaque state requires manual memory review")
    return retained_slots(obj)


def sliding_median(
    fn: Callable[..., Any], a: dict[str, Any], c: Checks, vectors: list[dict[str, Any]]
) -> None:
    even, warmup, interface = a.get("even"), a.get("warmup"), a.get("interface")
    if even not in ("mean", "lower", "upper") or a.get("even_policy_stated") is not True:
        raise ContractError("Declare original even-window median policy before tests")
    if (
        warmup not in ("partial", "full_only", "sentinel_until_full")
        or a.get("warmup_reviewed") is not True
    ):
        raise ContractError("Warm-up unspecified; do not choose a best-fitting policy")
    if warmup == "sentinel_until_full" and "sentinel" not in a:
        raise ContractError("Declare warm-up sentinel")
    if interface not in ("stream", "stateful"):
        raise ContractError("Declare stream or stateful interface")
    if interface == "stateful" and a.get("emission") not in ("update_return", "query"):
        raise ContractError("Declare stateful update-return or separate query emission")

    def make(values: Iterable[int], k: int) -> tuple[Any, Any]:
        if interface == "stream":
            output = invoke(fn, a.get("call"), {"stream": values, "k": k})
            return output, iter(output)
        obj = invoke(fn, a.get("construct"), {"k": k})

        def outputs() -> Iterable[Any]:
            for i, value in enumerate(values):
                updated = method(obj, a.get("update"), {"value": value})
                if warmup == "full_only" and i + 1 < k:
                    continue
                # Query only at the original interface's declared emission points.
                yield (
                    updated if a["emission"] == "update_return" else method(obj, a.get("query"), {})
                )

        return obj, iter(outputs())

    for item in vectors:
        if item["scope"] == "instrumented_memory_witness":
            continue

        def one(item: dict[str, Any] = item) -> None:
            _, output = make(iter(item["input"]["stream"]), item["input"]["k"])
            expected = item["expected"][even][warmup]
            expected = [
                a.get("sentinel") if isinstance(value, dict) else value for value in expected
            ]
            require(list(output) == expected, "wrong median values or declared warm-up emissions")

        c.check(item["id"], one, "declared_policy")

    for pattern in ("increasing", "decreasing", "all_equal", "alternating_extremes"):

        def memory(pattern: str = pattern) -> None:
            def stream() -> Iterable[int]:
                for i in range(8208):
                    yield (
                        i
                        if pattern == "increasing"
                        else -i
                        if pattern == "decreasing"
                        else 7
                        if pattern == "all_equal"
                        else (-1 if i % 2 else 1) * i
                    )

            obj, output = make(stream(), 17)
            if interface == "stream" and not isinstance(obj, types.GeneratorType):
                raise ContractError(
                    "Non-generator stream state requires manual review; "
                    "output collection growth is not algorithm-state proof"
                )
            sizes: list[int] = []
            count = 0
            try:
                for target in (128, 1024, 8192):
                    for _ in range(target - count):
                        next(output)
                    count = target
                    sizes.append(observed_state_slots(obj))
                c.measurements["median_retained_slots_" + pattern] = dict(
                    zip(("128", "1024", "8192"), sizes, strict=True)
                )
                require(
                    not (sizes[2] > max(128, sizes[0] * 8) and sizes[2] > sizes[1] * 4),
                    "linear stale-state growth witnessed",
                )
            finally:
                close = getattr(output, "close", None)
                if close is not None:
                    close()

        c.check("state_growth_" + pattern, memory, "memory_witness")


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
        namespace = vars(module)
        exec(compile(payload["code"], "<unchanged-answer>", "exec"), namespace)
        fn = namespace.get(name)
        if not callable(fn):
            raise ContractError("Reviewed entrypoint is not callable")
        case = payload["case"]
        novel = {
            "escaped_path": escaped_path,
            "lazy_merge": lazy_merge,
            "dependency_order": dependency_order,
            "dependency_invalidation": dependency_invalidation,
            "sliding_median": sliding_median,
        }
        if case in novel or case == "run_length":
            vectors = payload.get("vectors")
            if not isinstance(vectors, list) or not vectors:
                raise ContractError("Pinned prospective vectors required")
            vectors = cast(list[dict[str, Any]], vectors)
            if case == "run_length":
                run_length(namespace, a, checks, vectors)
            else:
                novel[case](fn, a, checks, vectors)
        elif case == "first_unique":
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
        checks.unscored("interface", str(exc), "mandatory")
    except BaseException as exc:
        checks.results.append(
            {
                "name": "program_execution",
                "scope": "mandatory",
                "status": "failed",
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}"[:400],
            }
        )
    return {
        "schema": 1,
        **checks.summary(),
        "elapsed_s": time.monotonic() - started,
        "limitations": [
            "Finite deterministic cases do not prove complexity or universal correctness.",
            "Unscored policies are not passing checks. "
            "Core subsets are not whole-program correctness.",
            "Container runtime is not answer/provider latency.",
            "Memory measurements observe accessible containers, not globals/closures/allocators; "
            "finite plateau is not boundedness proof.",
        ],
    }


if __name__ == "__main__":
    if not isolated_worker():
        raise SystemExit("Refusing generated-code execution outside the isolated container")
    payload = json.loads(sys.stdin.read())
    print(json.dumps(run(payload)))
