"""Bounded payload-free diagnostics shared by paid evaluation runners."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast


def sanitized_exception_tree(error: BaseException) -> dict[str, Any]:
    """Bounded diagnostics without exception messages, local values, or request payloads."""
    seen: set[int] = set()
    remaining = 32

    def visit(current: BaseException) -> dict[str, Any]:
        nonlocal remaining
        item: dict[str, Any] = {"type": type(current).__name__}
        if id(current) in seen or remaining == 0:
            return {**item, "truncated": True}
        seen.add(id(current))
        remaining -= 1
        if isinstance(current, OSError):
            for name in ("errno", "winerror"):
                value = getattr(current, name, None)
                if type(value) is int:
                    item[name] = value
        frames: list[dict[str, Any]] = []
        trace = current.__traceback__
        while trace is not None:
            frames.append(
                {"file": Path(trace.tb_frame.f_code.co_filename).name, "line": trace.tb_lineno}
            )
            trace = trace.tb_next
        if frames:
            item["frames"] = frames[-8:]
        if isinstance(current, BaseExceptionGroup):
            children = cast(BaseExceptionGroup[BaseException], current).exceptions
            item["children"] = [visit(child) for child in children[:8]]
            if len(children) > 8:
                item["omitted_children"] = len(children) - 8
        if current.__cause__ is not None:
            item["cause"] = visit(current.__cause__)
        elif current.__context__ is not None and not current.__suppress_context__:
            item["context"] = visit(current.__context__)
        return item

    return visit(error)
