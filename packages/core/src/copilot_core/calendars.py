"""Read-only Google/Outlook calendar adapters; no background jobs or session creation."""

import base64
import contextlib
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx2

Provider = Literal["google", "outlook"]
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
OUTLOOK_SCOPES = ("https://graph.microsoft.com/Calendars.ReadBasic", "offline_access")
MAX_PAGES = 5
MAX_EVENTS = 2000


class CalendarError(Exception):
    def __init__(self, message: str, *, reauthorize: bool = False, status: int = 502) -> None:
        super().__init__(message)
        self.reauthorize = reauthorize
        self.status = status


@dataclass(frozen=True)
class ProviderConfig:
    provider: Provider
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    tenant: str = "common"

    def __post_init__(self) -> None:
        target = urlsplit(self.redirect_uri)
        if target.scheme != "https" and not (
            target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("Calendar redirect URI must use HTTPS (or localhost for development)")
        if target.username or target.password or target.fragment or target.query:
            raise ValueError(
                "Calendar redirect URI must not contain credentials, query or fragment"
            )
        if not target.hostname or not re.fullmatch(r"[A-Za-z0-9.-]{1,128}", self.tenant):
            raise ValueError("Invalid calendar OAuth configuration")

    @property
    def authorize_endpoint(self) -> str:
        return (
            "https://accounts.google.com/o/oauth2/v2/auth"
            if self.provider == "google"
            else f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"
        )

    @property
    def token_endpoint(self) -> str:
        return (
            "https://oauth2.googleapis.com/token"
            if self.provider == "google"
            else f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
        )

    @property
    def scopes(self) -> tuple[str, ...]:
        return GOOGLE_SCOPES if self.provider == "google" else OUTLOOK_SCOPES


def configured(provider: Provider) -> ProviderConfig | None:
    prefix = f"COPILOT_CALENDAR_{provider.upper()}_"
    values = [
        os.environ.get(prefix + name, "").strip()
        for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI")
    ]
    if not all(values):
        return None
    try:
        return ProviderConfig(provider, *values, tenant=os.environ.get(prefix + "TENANT", "common"))
    except ValueError:
        return None


def authorization_url(config: ProviderConfig, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(config.scopes),
        "state": state,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    if config.provider == "google":
        query.update(access_type="offline", prompt="consent")
    else:
        query.update(response_mode="query", prompt="select_account")
    return config.authorize_endpoint + "?" + urlencode(query)


def state_key(state: str) -> str:
    return "calendar:oauth:" + hashlib.sha256(state.encode()).hexdigest()


def _timestamp(value: dict[str, Any], *, outlook: bool = False) -> str:
    raw = value.get("dateTime") or value.get("date")
    if not isinstance(raw, str):
        raise ValueError("Event time missing")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if value.get("dateTime") and not value.get("timeZone"):
            raise ValueError("Event timezone missing")
        zone = str(value.get("timeZone") or "UTC")
        # Graph is explicitly requested in UTC; do not guess Windows timezone names.
        if outlook and zone not in ("UTC", "Etc/UTC"):
            raise ValueError("Provider did not honor requested UTC timezone")
        try:
            parsed = parsed.replace(tzinfo=UTC if zone in ("UTC", "Etc/UTC") else ZoneInfo(zone))
        except ZoneInfoNotFoundError as error:
            raise ValueError("Event timezone unavailable") from error
    return parsed.astimezone(UTC).isoformat()


def normalize_event(
    provider: Provider, event: dict[str, Any], calendar_id: str
) -> dict[str, Any] | None:
    """Provider-expanded recurrence instances; cancelled/deleted rows never survive snapshots."""
    if event.get("status") == "cancelled" or event.get("isCancelled") or "@removed" in event:
        return None
    declined = (
        any(
            cast(dict[str, Any], item).get("self")
            and cast(dict[str, Any], item).get("responseStatus") == "declined"
            for item in event.get("attendees", [])
            if isinstance(item, dict)
        )
        if provider == "google"
        else event.get("responseStatus", {}).get("response") == "declined"
    )
    if declined:
        return None
    start = event.get("start")
    end = event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise CalendarError("Calendar returned an event without a valid time")
    try:
        starts_at = _timestamp(cast(dict[str, Any], start), outlook=provider == "outlook")
        ends_at = _timestamp(cast(dict[str, Any], end), outlook=provider == "outlook")
        if datetime.fromisoformat(ends_at) <= datetime.fromisoformat(starts_at):
            raise ValueError("End precedes start")
    except ValueError as error:
        raise CalendarError("Calendar returned an invalid event timezone or interval") from error
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise CalendarError("Calendar returned an event without identity")
    return {
        "calendar_id": calendar_id,
        "instance_id": event_id,
        "title": str(event.get("summary" if provider == "google" else "subject") or "Meeting")[
            :500
        ],
        "start": starts_at,
        "end": ends_at,
        "all_day": bool(cast(dict[str, Any], start).get("date") or event.get("isAllDay")),
        "series_id": event.get("recurringEventId" if provider == "google" else "seriesMasterId"),
    }


class CalendarClient:
    def __init__(self, config: ProviderConfig, http: httpx2.AsyncClient) -> None:
        self.config = config
        self.http = http

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx2.RequestError as error:
            raise CalendarError(
                "Calendar provider is unavailable; cached meetings remain available"
            ) from error
        if response.status_code >= 400:
            invalid_grant = False
            with contextlib.suppress(ValueError, AttributeError):
                invalid_grant = response.json().get("error") == "invalid_grant"
            reauthorize = response.status_code in (401, 403) or invalid_grant
            raise CalendarError(
                "Calendar permission expired or was revoked; reconnect"
                if reauthorize
                else "Calendar provider rate limit; try again later"
                if response.status_code == 429
                else "Calendar provider request failed; try again later",
                reauthorize=reauthorize,
                status=409 if reauthorize else 429 if response.status_code == 429 else 502,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise CalendarError("Calendar provider returned an invalid response") from error
        if not isinstance(body, dict):
            raise CalendarError("Calendar provider returned an invalid response")
        return cast(dict[str, Any], body)

    async def tokens(
        self,
        *,
        code: str | None = None,
        verifier: str | None = None,
        refresh_token: str | None = None,
    ) -> dict[str, Any]:
        data = {"client_id": self.config.client_id, "client_secret": self.config.client_secret}
        if refresh_token:
            data.update(grant_type="refresh_token", refresh_token=refresh_token)
        elif code and verifier:
            data.update(
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri=self.config.redirect_uri,
            )
        else:
            raise ValueError("Authorization code and verifier or refresh token required")
        if self.config.provider == "outlook":
            data["scope"] = " ".join(self.config.scopes)
        body = await self._json("POST", self.config.token_endpoint, data=data)
        access = body.get("access_token")
        refresh = body.get("refresh_token") or refresh_token
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise CalendarError(
                "Calendar did not grant offline access; reconnect", reauthorize=True, status=409
            )
        try:
            lifetime = min(max(int(body.get("expires_in", 3600)), 1), 86400)
        except (TypeError, ValueError) as error:
            raise CalendarError("Calendar returned an invalid token lifetime") from error
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": datetime.now(UTC).timestamp() + lifetime,
        }

    async def _pages(self, url: str, token: str, params: dict[str, str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        google = self.config.provider == "google"
        for _ in range(MAX_PAGES):
            body = await self._json(
                "GET",
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": 'outlook.timezone="UTC"',
                },
            )
            items = body.get("items" if google else "value", [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in cast(list[Any], items)
            ):
                raise CalendarError("Calendar provider returned an invalid collection")
            output.extend(cast(list[dict[str, Any]], items))
            if len(output) > MAX_EVENTS:
                raise CalendarError("Calendar response exceeds the synchronization limit")
            next_page = body.get("nextPageToken" if google else "@odata.nextLink")
            if not next_page:
                return output
            if google:
                params = {**params, "pageToken": str(next_page)}
            else:
                next_url = urlsplit(str(next_page))
                if (
                    next_url.scheme != "https"
                    or next_url.netloc != "graph.microsoft.com"
                    or not next_url.path.startswith("/v1.0/me/")
                    or next_url.fragment
                ):
                    raise CalendarError("Calendar returned an unsafe pagination URL")
                url, params = str(next_page), {}
        raise CalendarError("Calendar pagination exceeds the synchronization limit")

    async def calendars(self, token: str) -> list[dict[str, str]]:
        google = self.config.provider == "google"
        url = (
            "https://www.googleapis.com/calendar/v3/users/me/calendarList"
            if google
            else "https://graph.microsoft.com/v1.0/me/calendars"
        )
        rows = await self._pages(
            url,
            token,
            {"maxResults": "100", "minAccessRole": "reader"}
            if google
            else {"$top": "100", "$select": "id,name"},
        )
        return [
            {
                "id": str(row["id"]),
                "name": str(row.get("summary" if google else "name") or "Calendar")[:500],
            }
            for row in rows
            if row.get("id") and not row.get("deleted")
        ]

    async def events(
        self, token: str, calendar_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        google = self.config.provider == "google"
        calendar = quote(calendar_id, safe="")
        url = (
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"
            if google
            else f"https://graph.microsoft.com/v1.0/me/calendars/{calendar}/calendarView"
        )
        params = (
            {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "showDeleted": "true",
                "maxResults": "250",
            }
            if google
            else {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$top": "250",
                "$select": (
                    "id,subject,start,end,isAllDay,isCancelled,responseStatus,seriesMasterId"
                ),
            }
        )
        rows = await self._pages(url, token, params)
        # Last instance wins, including cancellation tombstones, before filtering.
        unique = {str(row.get("id", "")): row for row in rows}
        normalized = [
            normalize_event(self.config.provider, row, calendar_id) for row in unique.values()
        ]
        return [row for row in normalized if row is not None]

    async def revoke(self, token: str) -> bool:
        if self.config.provider == "outlook":
            # Graph has no least-privilege per-token revocation endpoint. Removing local
            # credentials is immediate; users may also remove consent in Microsoft My Apps.
            return False
        try:
            response = await self.http.post(
                "https://oauth2.googleapis.com/revoke", data={"token": token}
            )
        except httpx2.RequestError:
            return False
        return response.status_code == 200
