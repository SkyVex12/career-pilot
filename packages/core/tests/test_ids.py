"""UUIDv7 ids: version and variant bits, creation order, and deterministic ids for replays."""

import uuid

from copilot_core.ids import uuid7, uuid7_for


def test_uuid7_is_monotonic_within_a_millisecond() -> None:
    ids = [uuid7() for _ in range(50_000)]  # many per millisecond
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for u in ids[:100] + ids[-100:]:
        assert u.version == 7
        assert u.variant == uuid.RFC_4122


def test_uuid7_for_is_deterministic_and_time_ordered() -> None:
    a = uuid7_for(1_700_000_000_000, "q", "fast")
    assert a == uuid7_for(1_700_000_000_000, "q", "fast")
    assert a != uuid7_for(1_700_000_000_000, "q", "deep")
    assert a.version == 7
    assert uuid7_for(1_700_000_000_001, "q", "fast").int >> 80 == 1_700_000_000_001
