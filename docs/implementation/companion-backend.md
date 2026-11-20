# Mobile companion backend

The account signs in through existing email or provider authentication. API requests use the bearer token and `x-copilot-profile`. Attaching never invokes SessionManager.attach, acquires a capture lease, starts STT, or creates a model request. Disconnecting a display never ends the original call.

- `GET /v1/companion/sessions`: the selected profile's live session, only when its actual account matches the caller and its sticky private-context marker allows that account.
- `POST /v1/companion/sessions/{session_id}/attach` with `{ "device_name": "Phone" }`: creates a twelve-hour, account-owned attachment (at most five live attachments per session).
- `GET /v1/companion/attachments`: account-owned grants.
- `DELETE /v1/companion/attachments/{attachment_id}`: revokes one display.
- `DELETE /v1/companion/attachments`: revokes all of the account's displays (sign-out action).

Websocket `/v1/companion` authenticates from Authorization or `access_token` in the initial JSON message, never from a query parameter. Send `companion.join` with `session_id`, `attachment_id`, optional `last_seq`, and `client_version`. The server validates both the persisted grant and the live mirror, current profile access, session account, and sticky private-context account before output. Revocation and expiry are checked every feed iteration; an authorization database outage fails closed.

Server output: `companion.ready`, then `companion.snapshot` (`seq`, `messages`, `reset:true`), then original sequenced transcript/question/answer/status messages; `companion.host` describes capture connection, and `companion.ended` terminates observation. A reconnect resets the presentation from a bounded snapshot and retained events. Mobile should preserve local drafts/scroll separately. This uses existing session events and control/result streams with a separate same-account snapshot; it does not reuse guest-sharing grants or expose private content to guests.

Controls use `{ "type":"companion.command", "command_id":"UUID", "kind":"answer|followup|end" }`. Only followup accepts `text` (required, at most 4000 chars). End requires `confirmed:true` and optional `summarize`. The owning gateway rechecks attachment/account/lease and calls the existing question.force or session.end handler. Results are `companion.command.result` with command_id and `queued|executed|denied|expired|rate_limited|nothing_to_answer|uncertain|conflict`.

A command UUID is stored atomically with a payload digest before queuing. Retry the same UUID and payload to retrieve its result, including after reconnect. Reusing a UUID for a different action returns conflict. The owner claims once before execution; a crash in the narrow execution/ack window returns uncertain rather than generating twice. Commands expire after 30 seconds; queued does not mean executed. Existing generation rate limits still apply. Access tokens can be renewed with `auth.refresh` and may not change account.

Migration `0025_companions.sql` grants account RLS on attachments. No credentials or raw audio are stored in attachment rows. The desktop session's original cleanup/summarization remains authoritative.

Validation includes deterministic account/private-context boundaries, confirmation and message allowlisting, atomic deduplication, host rechecks, and a real PostgreSQL/Valkey grant-revocation lifecycle. Native mobile background/sleep and signed iOS/Android device acceptance remain release checks; this document does not claim those were exercised.
