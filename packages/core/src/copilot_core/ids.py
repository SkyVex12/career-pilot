"""UUIDv7 ids (ARCHITECTURE.md, "Conventions"): generated in the application, sortable by time.

Python 3.12 has no uuid.uuid7, so this follows RFC 9562 section 5.7: 48-bit Unix milliseconds,
version 7, 12 random bits, variant 0b10, 62 random bits. Ids from one process are also
monotonic (RFC 9562 section 6.2, method 2): within one millisecond the next id is the last one
plus a random step, so `order by id` is creation order even for rows made in a tight loop.
"""

import hashlib
import os
import threading
import time
import uuid

_lock = threading.Lock()
_last = 0


def uuid7() -> uuid.UUID:
    global _last
    with _lock:
        value = _uuid7(time.time_ns() // 1_000_000, int.from_bytes(os.urandom(10), "big")).int
        if value <= _last:
            # Same millisecond (or the clock stepped back): count up from the last id. The
            # random bits sit below the version and variant fields, which stay intact as long
            # as the step does not carry into them; 2^32 steps cannot within one millisecond.
            value = _last + 1 + (int.from_bytes(os.urandom(4), "big") & 0xFFFF)
            if (value >> 62) & 0b11 != 0b10:  # carried into the variant bits: next ms instead
                value = _uuid7((_last >> 80) + 1, int.from_bytes(os.urandom(10), "big")).int
        _last = value
        return uuid.UUID(int=value)


def uuid7_for(ms: int, *parts: str) -> uuid.UUID:
    """A deterministic UUIDv7: the given time, and "random" bits from a hash of `parts`.

    For rows written by replayable jobs whose source has no id of its own (an answer is one
    per question and pass), so a retry writes the same id and `on conflict` skips it.
    """
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return _uuid7(ms, int.from_bytes(digest[:10], "big"))


def _uuid7(ms: int, rand: int) -> uuid.UUID:
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)
