import json
from typing import Any

import pytest
from copilot_evals.live_answer_diagnostics import sanitized_exception_tree
from copilot_evals.live_answer_judge import failure_details
from copilot_evals.live_answer_replay import sanitized_exception_tree as replay_tree


class SensitiveError(Exception):
    def __str__(self) -> str:
        raise AssertionError("Do not stringify provider failures")


def test_messages_arguments_and_provider_payloads_never_persist() -> None:
    err: Any = SensitiveError("SECRET-message", {"api_key": "SECRET-key"})
    err.body = {"prompt": "SECRET-evidence"}
    err.headers = {"Authorization": "Bearer SECRET-token"}
    err.url = "https://example.invalid/SECRET-question"
    record = failure_details(err)
    assert record["failure_kind"] == "unexpected_error"
    assert record["exception"] == {"type": "SensitiveError"}
    assert "SECRET" not in json.dumps(record)


def test_nested_transport_root_cause_is_retained_without_message() -> None:
    root = ConnectionResetError(10054, "SECRET-payload")
    wrapped = RuntimeError("SECRET-sdk-message")
    wrapped.__cause__ = root
    record = failure_details(wrapped)
    assert record["failure_kind"] == "transport"
    assert record["exception"]["cause"] == {"type": "ConnectionResetError", "errno": 10054}
    assert "SECRET" not in json.dumps(record)


def test_suppressed_implicit_context_stays_hidden() -> None:
    err = ValueError("SECRET-result")
    err.__context__ = SensitiveError("SECRET-context")
    err.__suppress_context__ = True
    assert sanitized_exception_tree(err) == {"type": "ValueError"}


def test_exception_group_records_bounded_children_and_omissions() -> None:
    err = ExceptionGroup("SECRET-group", [ValueError("SECRET-child") for _ in range(20)])
    tree = sanitized_exception_tree(err)
    assert len(tree["children"]) == 8
    assert tree["omitted_children"] == 12
    assert "SECRET" not in json.dumps(tree)


def test_cyclic_cause_is_finite() -> None:
    a = RuntimeError("SECRET-a")
    b = OSError(5, "SECRET-b")
    a.__cause__ = b
    b.__cause__ = a
    tree = failure_details(a)["exception"]
    assert tree["cause"]["cause"] == {"type": "RuntimeError", "truncated": True}


def test_deep_chain_has_explicit_node_limit() -> None:
    err = RuntimeError("SECRET")
    for _ in range(200):
        parent = RuntimeError("SECRET")
        parent.__cause__ = err
        err = parent
    tree = sanitized_exception_tree(err)
    count = 1
    item = tree
    while "cause" in item:
        item = item["cause"]
        count += 1
    assert count == 33
    assert item["truncated"] is True
    assert len(json.dumps(tree)) < 2500


def test_trace_keeps_basename_and_line_but_no_locals_or_full_path() -> None:
    def fail() -> None:
        secret_value = "SECRET-local"
        raise ValueError(secret_value)
    tree: dict[str, Any] = {}
    try:
        fail()
    except ValueError as err:
        tree = sanitized_exception_tree(err)
    assert tree["frames"]
    assert all(set(frame) == {"file", "line"} for frame in tree["frames"])
    assert all("/" not in f["file"] and "\\" not in f["file"] for f in tree["frames"])
    assert "SECRET" not in json.dumps(tree)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "authentication"),
        (403, "permission"),
        (429, "rate_limit"),
        (503, "provider_server"),
        (404, "request_configuration"),
        (408, "provider_timeout"),
    ],
)
def test_diagnostics_preserve_existing_failure_classification(status: int, kind: str) -> None:
    err: Any = RuntimeError("SECRET")
    err.status_code = status
    record = failure_details(err)
    assert record["failure_kind"] == kind
    assert record["http_status"] == status
    assert record["exception"]["type"] == "RuntimeError"


def test_replay_uses_same_shared_helper() -> None:
    assert replay_tree is sanitized_exception_tree
