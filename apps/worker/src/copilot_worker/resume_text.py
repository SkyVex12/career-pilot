"""Bounded, isolated extraction of original text; never infer/OCR missing PDF words."""

import asyncio
import base64
import io
import json
import sys
from typing import Any

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 60000


def extract(block: dict[str, Any]) -> str:
    source = block["source"]
    if source.get("type") == "text":
        return str(source.get("data", ""))[:MAX_TEXT].strip()
    encoded = source.get("data", "")
    if not isinstance(encoded, str) or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 8:
        return ""
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MAX_FILE_BYTES or source.get("media_type") != "application/pdf":
        return ""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted or len(reader.pages) > 25:
        return ""
    chunks: list[str] = []
    size = 0
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > 2 * 1024 * 1024:
            return ""
        text = page.extract_text() or ""
        chunks.append(text[: MAX_TEXT - size])
        size += len(chunks[-1])
        if size >= MAX_TEXT:
            break
    return "\n".join(chunks)[:MAX_TEXT].strip()


async def original_text(block: dict[str, Any]) -> str:
    # Parsing runs in a killable subprocess, never in the API or worker event loop.
    data = json.dumps(block).encode()
    if len(data) > MAX_FILE_BYTES * 2:
        return ""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "copilot_worker.resume_text",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(20):
            output, _ = await process.communicate(data)
        if process.returncode or len(output) > MAX_TEXT * 12:
            return ""
        value = json.loads(output)
        return value if isinstance(value, str) else ""
    except (TimeoutError, ValueError):
        return ""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        value = extract(json.loads(sys.stdin.buffer.read(MAX_FILE_BYTES * 2 + 1)))
    except Exception:
        value = ""
    sys.stdout.write(json.dumps(value))
