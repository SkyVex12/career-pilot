"""Read-only meeting matching for provider-expanded, timezone-aware event instances."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Calendar timestamps must include a time zone")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CalendarMeeting:
    account_id: str
    calendar_id: str
    instance_id: str
    title: str
    start: datetime
    end: datetime
    cancelled: bool = False
    declined: bool = False
    all_day: bool = False
    series_id: str | None = None
    application_id: str | None = None

    def __post_init__(self) -> None:
        if _utc(self.end) <= _utc(self.start):
            raise ValueError("Meeting end must follow start")


@dataclass(frozen=True)
class ApplicationContext:
    application_id: str
    profile_id: str
    company: str
    job_title: str
    resume_id: str | None = None
    job_description_id: str | None = None


@dataclass(frozen=True)
class PreviousMeeting:
    profile_id: str
    account_id: str
    calendar_id: str
    series_id: str
    notes: str
    unfinished_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Preparation:
    meeting: CalendarMeeting
    candidates: tuple[ApplicationContext, ...]
    selected_application_id: str | None
    requires_confirmation: bool
    overlaps: bool
    previous: tuple[PreviousMeeting, ...]


def _words(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def prepare_meetings(
    meetings: list[CalendarMeeting],
    *,
    now: datetime,
    profile_id: str,
    selected_calendars: set[tuple[str, str]],
    applications: list[ApplicationContext],
    previous: list[PreviousMeeting] | None = None,
    horizon: timedelta = timedelta(minutes=30),
) -> list[Preparation]:
    """Title matches require confirmation. Only confirmed IDs may select context.

    Providers expand recurrences and resolve event time zones before calling.
    Selection includes account identity. All-day events are not call reminders.
    """
    instant = _utc(now)
    if horizon < timedelta(0):
        raise ValueError("Preparation horizon must be nonnegative")
    eligible: dict[tuple[str, str, str], CalendarMeeting] = {}
    for meeting in meetings:
        key = meeting.account_id, meeting.calendar_id, meeting.instance_id
        # Last provider instance wins, including cancellation tombstones.
        eligible[key] = meeting
    upcoming = sorted(
        (
            meeting
            for meeting in eligible.values()
            if (meeting.account_id, meeting.calendar_id) in selected_calendars
            and not (meeting.cancelled or meeting.declined or meeting.all_day)
            and _utc(meeting.end) > instant
            and _utc(meeting.start) <= instant + horizon
        ),
        key=lambda meeting: (
            _utc(meeting.start),
            meeting.account_id,
            meeting.calendar_id,
            meeting.instance_id,
        ),
    )
    scoped = [app for app in applications if app.profile_id == profile_id]
    result: list[Preparation] = []
    for meeting in upcoming:
        title = f" {_words(meeting.title)} "
        exact = [app for app in scoped if app.application_id == meeting.application_id]
        candidates = exact or [
            app
            for app in scoped
            if len(_words(app.company)) >= 3 and f" {_words(app.company)} " in title
        ]
        selected = exact[0].application_id if len(exact) == 1 else None
        overlaps = any(
            other is not meeting
            and _utc(other.start) < _utc(meeting.end)
            and _utc(meeting.start) < _utc(other.end)
            for other in upcoming
        )
        history = tuple(
            item
            for item in (previous or [])
            if meeting.series_id is not None
            and item.profile_id == profile_id
            and item.account_id == meeting.account_id
            and item.calendar_id == meeting.calendar_id
            and item.series_id == meeting.series_id
        )
        result.append(
            Preparation(
                meeting,
                tuple(candidates),
                selected,
                bool(candidates) and selected is None,
                overlaps,
                history,
            )
        )
    return result
