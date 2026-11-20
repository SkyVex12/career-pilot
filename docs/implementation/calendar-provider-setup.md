# Google Calendar and Outlook: deployment and verification

The calendar backend uses real provider authorization-code and read-only calendar APIs. No sample calendar data is installed. A provider is unavailable until its server configuration is complete and valid. Background call suggestions remain independent of calendar access.

## Server configuration

Apply migrations through `0023_calendars.sql` and `0024_private_context_sessions.sql` (or the migration introducing `sessions.private_context_account_id`) before deploying these routes. The API router is included at `/v1` without requiring a Career-OS link; every operation except the OAuth callback still requires the existing signed-in account and active-profile authorization.

For Google, configure a **Web application** OAuth client, enable the Google Calendar API, and register the exact backend callback URL:

- `COPILOT_CALENDAR_GOOGLE_CLIENT_ID`
- `COPILOT_CALENDAR_GOOGLE_CLIENT_SECRET`
- `COPILOT_CALENDAR_GOOGLE_REDIRECT_URI=https://API-HOST/v1/calendars/google/callback`

For Outlook, configure a Microsoft Entra **Web** application with delegated permissions and the exact callback URL:

- `COPILOT_CALENDAR_OUTLOOK_CLIENT_ID`
- `COPILOT_CALENDAR_OUTLOOK_CLIENT_SECRET`
- `COPILOT_CALENDAR_OUTLOOK_REDIRECT_URI=https://API-HOST/v1/calendars/outlook/callback`
- `COPILOT_CALENDAR_OUTLOOK_TENANT=common` (or the intended tenant ID/domain; account audience must match the app registration)

Use the deployment's secret-storage injection for client secrets. They belong only on the API server, never in desktop/frontend configuration or browser storage. HTTPS is required except localhost/127.0.0.1 development callbacks. Configure ingress/access logs to redact callback query strings; the application request logger records paths without queries. No separate callback CORS permission is needed for the provider's top-level redirect.

Google requests `calendar.calendarlist.readonly` and `calendar.events.readonly`, with offline access. Microsoft requests `Calendars.ReadBasic` and `offline_access`; meeting bodies, attachments and write privileges are unnecessary. Google consent verification or Microsoft tenant administrator approval may be necessary for production distribution. Configure these in the provider consoles; this implementation cannot provide production credentials or consent approval.

Official references: [Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server), [Google Calendar scopes](https://developers.google.com/workspace/calendar/api/auth), [Google CalendarList](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list), [Google recurring-instance listing](https://developers.google.com/workspace/calendar/api/v3/reference/events/list), [Microsoft authorization code and PKCE](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow), [Microsoft calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0).

## Flow and API contract

All paths below use `/v1`. Existing authorization/profile headers apply except to the public callback.

1. `GET /calendars` returns `items` with provider, availability, connected/status, calendar names/IDs, selected IDs, last successful sync, and a sanitized last error.
2. `POST /calendars/{google|outlook}/connect` returns `authorization_url`. Open that URL in the user's browser. The API stores a ten-minute, one-use random state bound to the account, profile, connection, and PKCE verifier. Only the state hash is used as the cache key. A newer connect invalidates older consent attempts for that connection.
3. The provider redirects to `/calendars/{provider}/callback`. The callback consumes state atomically, checks provider and current profile authorization, exchanges the code with PKCE, lists available calendars, and envelope-encrypts the tokens/cache using the account's existing data key. It never returns credentials to the desktop. Its static page asks the user to return to Ruby. Refresh `GET /calendars` when the workspace regains focus or through a user refresh control.
4. `PATCH /calendars/{provider}/selection` accepts `{ "calendar_ids": ["provider-calendar-id"] }`, with at most ten IDs drawn from that connection's returned calendars. Selection changes clear cached events.
5. `POST /calendars/{provider}/sync` performs an explicit bounded synchronization. No background scheduler is installed. A connection can refresh at most once per five minutes. Synchronization covers yesterday through fourteen days ahead, at most five pages per provider collection and 2,000 events overall. Limit overflow is an error, never a silently incomplete success. Successful full snapshots replace cached instances, so removed events cannot remain from a previous snapshot. Calls are read-only.
6. `GET /meeting-preparation` returns upcoming/ongoing meetings in the next thirty minutes with title, aware UTC times, overlap indicators, profile-scoped application/resume/JD candidates, confirmed selection, previous recurring notes/actions, and connection freshness/error states. Title matches always require user confirmation. No profile switch, session creation, recording, or auto-join occurs.
7. `PUT /calendars/{provider}/context` saves explicit context for a cached selected meeting: `{ "calendar_id": "...", "instance_id": "...", "application_id": "UUID or null", "session_id": "UUID or null", "unfinished_actions": ["..."] }`. Application/session IDs must belong to the current profile. Private session notes additionally require the current account. Saved context/actions are encrypted; recurring history is scoped to connection, calendar, series and profile. Unfinished actions are explicit user input; no action extraction or model request is made.
8. `DELETE /calendars/{provider}` deletes local credentials, cached calendar metadata/events, and saved context immediately, then attempts provider revocation. The response reports `provider_revoked` and `provider_consent_removal_required`. Local deletion succeeds even when credential decryption or the provider is unavailable. Pending callbacks cannot recreate a disconnected connection.

Reauthorization clears selected calendars, cached events and confirmed meeting associations because the user may choose a different external account; no extra identity permission is requested just to infer account identity.

## Failure and security behavior

Expired access tokens refresh under a row lock; rotated credentials commit before event fetching so a later provider failure cannot roll them back. Revocation/expired permission marks the connection `reauth_required`; transient/offline/rate-limit failures retain the last encrypted successful cache with an explicit error state. Sync and disconnect/reconnect races cannot overwrite a changed connection. Profile authorization is checked at consent and on every authenticated operation.

Google revocation uses its token-revocation endpoint. Microsoft does not expose an equivalent per-refresh-token revocation endpoint under these minimal calendar scopes. Disconnect deletes the local tokens and cache, and reports that the user may remove the application's consent in Microsoft My Apps/account permissions. The app does not request broad directory permissions or revoke all of a user's Microsoft sessions.

Graph pagination URLs are restricted to the HTTPS Microsoft Graph origin and `/v1.0/me/` path; bearer tokens cannot follow a provider-supplied external next link. HTTP redirects are disabled. Upstream error bodies, authorization codes, tokens, and client secrets are never included in user-facing errors. Calendar token/cache columns and saved associations use separate authenticated-encryption namespaces and row IDs; account RLS plus an explicit profile filter bounds access.

Google recurring events are expanded with `singleEvents=true`; Outlook uses `calendarView`. Cancelled/deleted and declined instances are excluded; all-day entries are retained in the cache but do not become call reminders. Timezone offsets are normalized to UTC, with explicit Graph UTC responses; invalid or missing timestamps/timezones fail synchronization rather than silently attaching the wrong meeting context. Meeting links are not required.

## Validation evidence and remaining setup

Executable on this host: 23 calendar adapter/policy tests, plus the earlier 25 call-policy/meeting-preparation tests, all passing. Ruff and Pyright pass for the new calendar core/routes/tests.

The real database/RLS/encryption lifecycle test is `apps/api/tests/test_calendar_stack.py`. It passed against the restored local PostgreSQL/Valkey stack on 2026-11-19, covering migrations, encrypted tokens/cache, one-use consent state, account isolation, sync, preparation, and disconnect despite provider revoke failure. Real Google/Microsoft credentials and external consent were not available. Re-run against the repository local stack before release:

`python -m pytest apps/api/tests/test_calendar_stack.py -q`

Real Google and Microsoft consent, tenant policies, token refresh/revocation, and provider data require configured production/test OAuth applications and user accounts. No real external calendar consent or real meeting test was performed by this implementation task.
