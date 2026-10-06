"""Load a local .env into the process environment, for development entry points.

Settings classes read .env themselves, but boto3 and the Bedrock client read AWS settings from
the process environment only. Variables already set win over the file.
"""

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value
