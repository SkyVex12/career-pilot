"""copilot_core.logs: JSON lines with the bound ids, context through asyncio tasks, the log
file per run, and the format chosen inside AWS."""

import asyncio
import io
import json
import logging
from pathlib import Path

import pytest

from copilot_core import logs


@pytest.fixture
def capture():
    """The root logger configured as in a service, writing JSON to a buffer."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logs.JsonFormatter("test"))
    handler.addFilter(logs.ContextFilter())
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    root.handlers, root.level = saved


def test_every_line_has_a_session_id_field(capture) -> None:
    log = logging.getLogger("t")
    log.info("outside")
    with logs.bind(session_id="s-1", user_id="u-1"):
        log.info("inside %d", 2)
        with logs.bind(job="session.flush"):
            log.warning("nested")
    log.info("after")
    lines = capture()
    assert [line["session_id"] for line in lines] == [None, "s-1", "s-1", None]
    assert lines[1]["msg"] == "inside 2"
    assert lines[1]["user_id"] == "u-1"
    assert lines[2]["job"] == "session.flush"
    assert "job" not in lines[3]
    assert lines[0]["service"] == "test"
    assert lines[0]["level"] == "INFO"


async def test_tasks_keep_the_ids_of_the_context_that_started_them(capture) -> None:
    log = logging.getLogger("t")

    async def work() -> None:
        await asyncio.sleep(0)
        log.info("in task")

    async def session(sid: str) -> None:
        logs.set_ids(session_id=sid)
        await asyncio.create_task(work())

    await asyncio.gather(session("a"), session("b"))
    log.info("caller")
    assert sorted(line["session_id"] or "-" for line in capture()) == ["-", "a", "b"]


def test_exceptions_are_kept(capture) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    [line] = capture()
    assert "ValueError: boom" in line["exc"]


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown log field"), logs.bind(transcript="x"):
        pass


def test_configure_json_inside_aws_and_a_file_per_run(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        env = {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x",
               "COPILOT_LOG_DIR": str(tmp_path)}  # fmt: skip
        path = logs.configure("gateway", env)
        assert path is not None
        assert path.parent == tmp_path
        assert path.name.startswith("gateway-")
        assert isinstance(root.handlers[0].formatter, logs.JsonFormatter)
        with logs.bind(session_id="s-9"):
            logging.getLogger("t").info("hello")
        for h in root.handlers:
            h.flush()
        [line] = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
        assert (line["session_id"], line["service"], line["msg"]) == ("s-9", "gateway", "hello")
        # Off AWS the console is text, with the ids at the end of the line.
        logs.configure("api", {})
        assert isinstance(root.handlers[0].formatter, logs.TextFormatter)
        assert len(root.handlers) == 1
    finally:
        for h in root.handlers:
            h.close()
        root.handlers, root.level = saved
