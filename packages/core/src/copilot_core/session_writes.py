"""Content-free authorization fingerprints for the existing retained session log."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def entry_hash(entry: Mapping[str, Any]) -> str:
    data = {k: v for k, v in entry.items() if k not in {"writer_generation", "writer_batch_id"}}
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
