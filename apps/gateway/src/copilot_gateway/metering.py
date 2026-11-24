"""Confirmed connected host time, independent of speech and the system wall clock.

Only emitted intervals can become usage. An attachment never resumes a previous clock:
reconnect gaps and an unconfirmed tail lost in a process crash cannot be inferred later.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

METERING_VERSION = "connected-v1"


@dataclass(frozen=True)
class LivePrice:
    version: str
    currency: str
    rate_minor_per_hour: int

    def state_fields(self) -> dict[str, str]:
        return {
            "metering_version": METERING_VERSION,
            "billing_price_version": self.version,
            "billing_currency": self.currency,
            "billing_rate_minor_per_hour": str(self.rate_minor_per_hour),
        }


@dataclass
class _Attachment:
    epoch: UUID
    payer: UUID
    anchor: datetime
    monotonic_start: float
    emitted_ms: int = 0


class ConnectedLiveMeter:
    def __init__(
        self,
        session_id: UUID,
        price: LivePrice,
        record: Callable[[Mapping[str, Any]], None],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_id = session_id
        self.price = price
        self.record = record
        self.monotonic = monotonic
        self.utc_now = utc_now
        self._attachment: _Attachment | None = None
        self._utc_anchor: datetime | None = None
        self._monotonic_anchor = 0.0

    @property
    def active(self) -> bool:
        return self._attachment is not None

    def start(self, payer: UUID) -> None:
        # A replacement must close its old attachment explicitly before starting another.
        if self.active:
            raise RuntimeError("A connected usage clock is already running")
        now = self.monotonic()
        if self._utc_anchor is None:
            utc = self.utc_now().astimezone(UTC)
            self._utc_anchor = utc.replace(microsecond=(utc.microsecond // 1000) * 1000)
            self._monotonic_anchor = now
        # Keep one UTC mapping for this process's session. Re-reading wall time at each
        # reconnect could overlap confirmed intervals after an NTP/backward clock change.
        anchor = self._utc_anchor + timedelta(
            milliseconds=max(0, int((now - self._monotonic_anchor) * 1000))
        )
        self._attachment = _Attachment(uuid4(), payer, anchor, now)

    def flush(self) -> None:
        attachment = self._attachment
        if attachment is not None:
            self._record(attachment)

    def stop(self, *, discard: bool = False) -> None:
        attachment, self._attachment = self._attachment, None
        if attachment is not None and not discard:
            self._record(attachment)

    def _record(self, attachment: _Attachment) -> None:
        end_ms = max(
            attachment.emitted_ms,
            int((self.monotonic() - attachment.monotonic_start) * 1000),
        )
        start_ms = attachment.emitted_ms
        if end_ms == start_ms:
            return
        # Commit the cursor before calling back: retention failure may synchronously stop
        # the meter. The writer retries the exact serialized event rather than recreating it.
        attachment.emitted_ms = end_ms
        self.record(
            {
                "kind": "usage.interval",
                "event_id": str(uuid5(attachment.epoch, f"{start_ms}:{end_ms}")),
                "session_id": str(self.session_id),
                "meter_epoch": str(attachment.epoch),
                "metering_version": METERING_VERSION,
                "started_at": (attachment.anchor + timedelta(milliseconds=start_ms)).isoformat(),
                "ended_at": (attachment.anchor + timedelta(milliseconds=end_ms)).isoformat(),
                "duration_ms": end_ms - start_ms,
                "payer_account_id": str(attachment.payer),
                "price_version": self.price.version,
                "currency": self.price.currency,
                "rate_minor_per_hour": self.price.rate_minor_per_hour,
                "service": "live",
            }
        )
