from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from copilot_core.calendars import (
    CalendarClient,
    CalendarError,
    Provider,
    ProviderConfig,
    authorization_url,
    configured,
    normalize_event,
)


def config(provider: Provider = "google") -> ProviderConfig:
    return ProviderConfig(
        provider, "client", "server-secret", "https://api.example.com/v1/calendars/callback"
    )


def event(identifier="event", **over):
    return {
        "id": identifier,
        "summary": "Interview",
        "start": {"dateTime": "2026-11-01T01:15:00-05:00"},
        "end": {"dateTime": "2026-11-01T01:45:00-05:00"},
        **over,
    }


@pytest.mark.parametrize("provider", ["google", "outlook"])
def test_authorization_is_read_only_pkce_and_secrets_stay_server_side(provider) -> None:
    url = authorization_url(config(provider), "state-value", "v" * 64)
    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["state-value"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert "server-secret" not in url
    assert "write" not in query["scope"][0].lower()
    assert "server-secret" not in repr(config(provider))


def test_missing_or_unsafe_configuration_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("COPILOT_CALENDAR_GOOGLE_CLIENT_SECRET", raising=False)
    assert configured("google") is None
    with pytest.raises(ValueError, match="HTTPS"):
        ProviderConfig("google", "client", "secret", "http://external.example/callback")
    with pytest.raises(ValueError, match="configuration"):
        ProviderConfig("outlook", "client", "secret", "https://example/callback", "../evil")


def test_cancellations_declines_and_recurring_timezones() -> None:
    assert normalize_event("google", event(status="cancelled"), "cal") is None
    assert (
        normalize_event(
            "google", event(attendees=[{"self": True, "responseStatus": "declined"}]), "cal"
        )
        is None
    )
    normalized = normalize_event("google", event(recurringEventId="series"), "cal")
    assert normalized is not None
    assert normalized["start"] == "2026-11-01T06:15:00+00:00"
    assert normalized["series_id"] == "series"
    assert normalize_event("outlook", event(isCancelled=True), "cal") is None
    assert normalize_event("outlook", event(responseStatus={"response": "declined"}), "cal") is None


def test_invalid_time_does_not_silently_mismatch_context() -> None:
    with pytest.raises(CalendarError, match="timezone"):
        normalize_event(
            "outlook",
            event(start={"dateTime": "2026-11-01T01:00:00", "timeZone": "Pacific Standard Time"}),
            "cal",
        )
    with pytest.raises(CalendarError, match="timezone"):
        normalize_event("google", event(end={"dateTime": "2026-10-01T00:00:00Z"}), "cal")


async def test_refresh_rotation_and_omitted_refresh_token() -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        tokens = await CalendarClient(config(), http).tokens(refresh_token="kept-refresh")
    assert tokens["refresh_token"] == "kept-refresh"
    assert tokens["access_token"] == "new-access"
    assert b"refresh_token=kept-refresh" in calls[0].content


async def test_authorization_exchange_requires_offline_refresh() -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"access_token": "access"})
        )
    ) as http:
        with pytest.raises(CalendarError, match="offline") as failure:
            await CalendarClient(config(), http).tokens(code="code", verifier="verifier")
    assert failure.value.reauthorize


@pytest.mark.parametrize(
    ("status", "body", "reauthorize"),
    [
        (400, {"error": "invalid_grant", "detail": "secret"}, True),
        (401, {}, True),
        (403, {}, True),
        (429, {}, False),
        (500, {}, False),
    ],
)
async def test_provider_failures_are_sanitized(status, body, reauthorize) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(status, json=body))
    ) as http:
        with pytest.raises(CalendarError) as failure:
            await CalendarClient(config(), http).calendars("secret-token")
    assert failure.value.reauthorize == reauthorize
    assert "secret" not in str(failure.value)


async def test_pagination_expands_recurrence_deduplicates_cancellation_and_escapes_ids() -> None:
    calls = []

    def handle(request):
        calls.append(request)
        if "pageToken" not in request.url.params:
            return httpx2.Response(200, json={"items": [event()], "nextPageToken": "page2"})
        return httpx2.Response(200, json={"items": [event(status="cancelled"), event("another")]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        now = datetime.now(UTC)
        result = await CalendarClient(config(), http).events(
            "access", "a/b@example", now, now + timedelta(days=1)
        )
    assert [row["instance_id"] for row in result] == ["another"]
    assert calls[0].url.params["singleEvents"] == "true"
    assert b"a%2Fb%40example" in calls[0].url.raw_path
    assert all(request.method == "GET" for request in calls)


async def test_graph_nextlink_cannot_exfiltrate_tokens() -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(
            200, json={"value": [], "@odata.nextLink": "https://evil.example/steal"}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        with pytest.raises(CalendarError, match="unsafe"):
            await CalendarClient(config("outlook"), http).calendars("secret-token")
    assert len(calls) == 1


async def test_pagination_is_bounded_instead_of_silently_truncated() -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(200, json={"items": [], "nextPageToken": "again"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        with pytest.raises(CalendarError, match="limit"):
            await CalendarClient(config(), http).calendars("access")
    assert len(calls) == 5


async def test_offline_and_outlook_disconnect_are_honest() -> None:
    def offline(request):
        raise httpx2.ConnectError("private provider detail", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(offline)) as http:
        with pytest.raises(CalendarError, match="cached"):
            await CalendarClient(config(), http).calendars("access")
        assert not await CalendarClient(config(), http).revoke("refresh")
        assert not await CalendarClient(config("outlook"), http).revoke("refresh")


async def test_graph_utc_response_without_tzdata_requirement() -> None:
    value = normalize_event(
        "outlook",
        {
            "id": "instance",
            "subject": "Meeting",
            "start": {"dateTime": "2026-11-01T06:15:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-01T06:45:00.0000000", "timeZone": "UTC"},
            "responseStatus": {"response": "accepted"},
        },
        "calendar",
    )
    assert value is not None
    assert value["start"] == "2026-11-01T06:15:00+00:00"
