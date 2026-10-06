"""UUIDv7 ids (ARCHITECTURE.md, "Conventions"): generated in the application, sortable by time.

Python 3.12 has no uuid.uuid7, so this follows RFC 9562 section 5.7: 48-bit Unix milliseconds,
version 7, 12 random bits, variant 0b10, 62 random bits.
"""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)
