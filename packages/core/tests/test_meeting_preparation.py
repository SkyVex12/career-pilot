from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from copilot_core.meeting_preparation import (
    ApplicationContext,
    CalendarMeeting,
    PreviousMeeting,
    prepare_meetings,
)

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
MEETING = CalendarMeeting(
    "account",
    "calendar",
    "instance",
    "Interview with Acme",
    NOW + timedelta(minutes=5),
    NOW + timedelta(minutes=35),
    series_id="series",
)
APP = ApplicationContext("app", "profile", "Acme", "Engineer", "resume", "jd")


def prepare(
    meetings: list[CalendarMeeting],
    *,
    now: datetime = NOW,
    applications: list[ApplicationContext] | None = None,
    previous: list[PreviousMeeting] | None = None,
    horizon: timedelta = timedelta(minutes=30),
):
    return prepare_meetings(
        meetings,
        now=now,
        profile_id="profile",
        selected_calendars={("account", "calendar")},
        applications=[APP] if applications is None else applications,
        previous=previous,
        horizon=horizon,
    )


def test_title_suggestion_requires_confirmation_and_never_crosses_profile() -> None:
    result = prepare(
        [MEETING], applications=[APP, replace(APP, profile_id="other", application_id="x")]
    )
    assert result[0].candidates == (APP,)
    assert result[0].requires_confirmation
    assert result[0].selected_application_id is None
    assert prepare([replace(MEETING, title="Acmeology interview")])[0].candidates == ()
    result = prepare(
        [replace(MEETING, application_id="x")],
        applications=[replace(APP, profile_id="other", application_id="x")],
    )
    assert result[0].selected_application_id is None
    assert result[0].candidates == ()


def test_confirmed_mapping_and_ambiguous_applications() -> None:
    result = prepare([replace(MEETING, application_id="app")])[0]
    assert result.selected_application_id == "app"
    assert not result.requires_confirmation
    result = prepare([MEETING], applications=[APP, replace(APP, application_id="app2")])[0]
    assert len(result.candidates) == 2
    assert result.requires_confirmation


@pytest.mark.parametrize(
    "change",
    [
        {"cancelled": True},
        {"declined": True},
        {"all_day": True},
        {"calendar_id": "unselected"},
        {"account_id": "other"},
        {"start": NOW - timedelta(hours=1), "end": NOW},
        {"start": NOW + timedelta(hours=1), "end": NOW + timedelta(hours=2)},
    ],
)
def test_ineligible_events_are_excluded(change: dict[str, object]) -> None:
    assert prepare([replace(MEETING, **change)]) == []


def test_cancellation_tombstone_and_overlapping_instances() -> None:
    assert prepare([MEETING, replace(MEETING, cancelled=True)]) == []
    result = prepare([MEETING, replace(MEETING, instance_id="second")])
    assert len(result) == 2
    assert all(item.overlaps for item in result)
    # Adjacent meetings do not overlap.
    result = prepare(
        [
            MEETING,
            replace(
                MEETING,
                instance_id="second",
                start=MEETING.end,
                end=MEETING.end + timedelta(minutes=30),
            ),
        ],
        horizon=timedelta(hours=1),
    )
    assert not any(item.overlaps for item in result)


def test_timezone_dst_offsets_and_naive_rejection() -> None:
    # The repeated 01:15 hour on a fall-back day represents different instants.
    first = replace(
        MEETING,
        start=datetime(2026, 11, 1, 1, 15, tzinfo=timezone(timedelta(hours=-5))),
        end=datetime(2026, 11, 1, 1, 45, tzinfo=timezone(timedelta(hours=-5))),
    )
    second = replace(
        first,
        instance_id="second",
        start=first.start.replace(tzinfo=timezone(timedelta(hours=-6))),
        end=first.end.replace(tzinfo=timezone(timedelta(hours=-6))),
    )
    assert [item.meeting.instance_id for item in prepare([second, first])] == ["instance"]
    with pytest.raises(ValueError, match="time zone"):
        replace(MEETING, start=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="time zone"):
        prepare([MEETING], now=NOW.replace(tzinfo=None))


def test_recurring_history_is_scoped_to_profile_account_calendar_and_series() -> None:
    note = PreviousMeeting(
        "profile", "account", "calendar", "series", "prior notes", ("follow up",)
    )
    history = [
        note,
        replace(note, profile_id="other"),
        replace(note, account_id="other"),
        replace(note, calendar_id="other"),
        replace(note, series_id="other"),
    ]
    assert prepare([MEETING], previous=history)[0].previous == (note,)
    assert prepare([replace(MEETING, series_id=None)], previous=history)[0].previous == ()
