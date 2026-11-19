"""Local-only Sensei export inspection. No provider calls or raw-text output.

The observed export has four lines per record, but does not label either text
field or identify the meaning of its timestamp. Semantic parsing requires the
caller to confirm the layout; inspection never infers speaker identity.
"""

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Record:
    record_id: int
    timestamp: datetime
    first_text: str = field(repr=False)
    second_text: str = field(repr=False)


@dataclass(frozen=True)
class Turn:
    record_id: int
    timestamp: datetime
    offset_s: float
    speaker: Literal["interviewer"]
    question: str = field(repr=False)
    suggestion: str = field(repr=False)


def inspect_export(text: str, *, year: int) -> list[Record]:
    """Validate structure without including any input text in errors."""
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or len(lines) % 4:
        raise ValueError("Expected nonempty four-line records; layout needs confirmation")
    records: list[Record] = []
    seen: set[int] = set()
    for start in range(0, len(lines), 4):
        identifier, stamp, first, second = lines[start : start + 4]
        number = start // 4 + 1
        if not re.fullmatch(r"#[0-9]+", identifier):
            raise ValueError(f"Record {number}: invalid identifier field")
        record_id = int(identifier[1:])
        if record_id in seen:
            raise ValueError(f"Record {number}: duplicate identifier")
        seen.add(record_id)
        try:
            timestamp = datetime.strptime(f"{year} {stamp}", "%Y %b %d, %I:%M:%S %p")
        except ValueError:
            raise ValueError(f"Record {number}: invalid timestamp field") from None
        if records and timestamp < records[-1].timestamp:
            raise ValueError(f"Record {number}: time moved backwards; do not silently reorder")
        if not first.strip() or not second.strip():
            raise ValueError(f"Record {number}: empty text field")
        records.append(Record(record_id, timestamp, first, second))
    return records


def parse_turns(records: list[Record], *, confirmed_question_then_suggestion: bool) -> list[Turn]:
    """Only use after confirming no candidate speech occurs in the first field."""
    if not confirmed_question_then_suggestion:
        raise ValueError("Confirm field roles and exclusion of candidate speech before replay")
    if not records:
        return []
    return [
        Turn(
            record.record_id,
            record.timestamp,
            (record.timestamp - records[0].timestamp).total_seconds(),
            "interviewer",
            record.first_text,
            record.second_text,
        )
        for record in records
    ]


def metadata(records: list[Record]) -> dict[str, object]:
    """Allowlisted output: no text, excerpts, dates, or file paths."""
    return {
        "records": len(records),
        "fields": ["record_id", "timestamp", "unlabeled_text_1", "unlabeled_text_2"],
        "unique_ids": len({record.record_id for record in records}),
        "duplicate_timestamps": len(records) - len({record.timestamp for record in records}),
        "span_seconds": (
            (records[-1].timestamp - records[0].timestamp).total_seconds() if records else 0
        ),
        "speaker_labels": False,
        "timestamp_semantics": "unconfirmed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        records = inspect_export(args.export.read_text(encoding="utf-8-sig"), year=args.year)
    except (OSError, UnicodeError):
        print(json.dumps({"error": "Cannot read export as UTF-8"}))
        return 1
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(metadata(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
