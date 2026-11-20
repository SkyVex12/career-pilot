# Context from my AI implementation

Updated 2026-11-19. This is a bounded publishing service, not an importer of private chat histories or source credentials. Codex CLI 0.152.1 has completed real local HTTP MCP read/publish/refresh runs against a fictional account and migrated disposable database, including a scoped OAuth-issued token. Remote consumer-host/browser onboarding remains deployment verification.

## Contract and authorization

The API router `/v1/personal-ai` uses the existing bearer authentication. REST request creation uses the selected profile header and verifies company/application ownership. Private tables use **account_id** RLS, independent of shared profile access; sealed request and version payloads use the account data key. MCP derives the profile and target from the stored request, then rechecks current profile membership. Generated text cannot choose an identity or target.

`POST /requests` accepts title, purpose (`call`, `practice`, `ask`), one optional employer_id/job_id, questions and an ordered date_from/date_to range of at most 366 days. `GET /requests` returns context cards and active briefs; `GET /requests/{id}` includes the preparation contract. `POST /requests/{id}/refresh` accepts expected_revision and produces a new request revision while preserving the usable brief. `POST /requests/{id}/publish` is the visibly attributed `paste` fallback using the same Publication validator. `PATCH /requests/{id}/items/{item_id}` accepts expected_version_id and text (null removes); `DELETE /requests/{id}` removes the request and all encrypted versions.

Brief items have stable IDs, category, text, provenance (`sourced`, `user_report`, `inference`) and source IDs. Sources preserve title, attribution, optional HTTP(S) link and source date. Preparation time and coverage gaps are separate. Sourced items require known sources, but schema/URL validity does not certify truth. Text is data, never execution authority. The brief is limited to 32 KiB, 40 items and 30 sources; account requests and per-request versions are bounded. Corrections survive replacement with the same item ID, including explicit removals. Correction text is a user report, not retroactively sourced evidence.

Row locks serialize publication and correction. Obsolete request revisions and conflicting idempotency keys fail without replacing active content. Exact duplicate submissions return their original version. Current retrieval only exposes active versions; deleted/replaced versions are unavailable as citations. Refresh performs no external research or paid model call.

## MCP transport

`POST /v1/personal-ai/mcp` supports stateless Streamable HTTP JSON responses with the **2025-11-25 compatibility protocol**, initialize, ping, tools/list and tools/call. It exposes exactly `get_context_request(request_id)` and `publish_context_brief(request_id, request_revision, idempotency_key, brief)`. Browser Origin is checked; requests are bounded to 64 KiB; every message authenticates. GET/SSE sessions, server-initiated tools and arbitrary execution are not implemented.

Current [MCP transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) and [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) were checked during implementation. The newer revision changes per-request metadata; this implementation deliberately advertises the earlier compatibility revision documented in [2025 tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools). Do not advertise native 2026 protocol support. Remote deployment requires a stable HTTPS endpoint reachable while the host invokes tools. Existing bearer-token configuration can support a compatible client; A bounded OAuth authorization server supports pre-registered public clients, explicit email sign-in/consent, PKCE S256 and MCP-only scopes. It does not advertise DCR or client metadata URL fetching. Use the copy request / paste brief workflow when no authenticated compatible host is configured.

## Live and historical privacy

Set `COPILOT_PERSONAL_AI_API_URL` to the trusted API origin to enable gateway reuse. The gateway calls the normal account-authenticated API with the selected profile, using the current bearer token in memory only. Each answer revalidates active briefs within a 350 ms default budget; it never waits for external AI preparation or reuses a stale brief on API failure. Direct prepared/cache reuse is disabled on this configured path. Ask/practice use the same private retrieval helpers and account boundary.

Before returning private content, the API durably creates/marks the session with private_context_account_id, and mirrors a sticky sharing-off marker. It serializes against new guest invites; existing admitted guests prevent private loading. The gateway disables guest sharing and guest snapshots, rejects a different account on resume/token refresh, and reports active version IDs through memory status. Deleting a brief removes future retrieval; it never clears the privacy marker on already generated artifacts. History detail/transcript/replay/notes/list/delete enforce account access; private standup outputs do not automatically create shared-company commitments.

## Verification and release gaps

Deterministic tests exercise bounds, provenance/citation preservation, correction survival, idempotency, stale/conflicting publications, encryption owner, wrong-profile/account denial, MCP authentication/envelopes/discovery/successful publication, sticky live privacy, guest exclusion and historical privacy. The real-stack test `apps/api/tests/test_personal_ai_stack.py` covers the complete authenticated request/publish/refresh/correct/remove path against migrated PostgreSQL/RLS and encryption. It passed against the local Docker stack on 2026-11-19 after PostgreSQL/Valkey became available. The focused combined personal-AI unit, private live context, call language and worker session-log run passed 58 tests. Desktop `tests/personal-ai.test.tsx` passed three fallback/retry, correction/removal and failed-refresh flows; desktop typecheck passed. The real local Codex host run and scoped-token validation described below passed; remote ChatGPT onboarding remains unverified.

Release verification still requires remote host browser consent and deployment reachability/sleep-wake checks, real permitted source gathering (the local host test deliberately uses fictional user reports only), and provider-specific confirmation/retention review. No host plan is claimed free or unlimited; host preparation and Career Pilot live usage remain separate costs.


## OAuth deployment and client setup

Configure `COPILOT_PERSONAL_AI_OAUTH_ISSUER` as the canonical externally reachable HTTPS API origin, with no path, query or fragment. Empty disables OAuth discovery/authorization. `COPILOT_EMAIL_SIGN_IN=true` and the normal deployment email sender must work for browser sign-in. The existing API verifier/authentication remains unchanged; MCP opaque tokens cannot authenticate ordinary application routes. The MCP router separately enforces the existing required Career-OS link policy.

`COPILOT_PERSONAL_AI_OAUTH_CLIENTS` is a JSON object mapping administrator-approved client IDs to redirect URI arrays. For example:

```json
{"codex-desktop":["http://127.0.0.1/callback"]}
```

Only HTTPS redirects match exactly. Literal `127.0.0.1`/`[::1]` HTTP loopback callbacks permit the listener port to vary as RFC 8252 requires; scheme, literal host, path and query must match registration. There are no wildcard hosts, redirect prefixes, arbitrary metadata fetches or dynamic registration. Register ChatGPT's exact callback shown in its MCP connection management page, under the chosen pre-registered public client ID. Do not guess a ChatGPT callback or claim the remote connection is configured before registration and reachable deployment exist.

For Codex, add the MCP URL with `--oauth-client-id codex-desktop`, register the callback the CLI prints, and request `context:read,context:publish` during login. A representative client configuration is:

```toml
[mcp_servers.career_pilot]
url = "https://YOUR_API_ORIGIN/v1/personal-ai/mcp"
scopes = ["context:read", "context:publish"]

[mcp_servers.career_pilot.oauth]
client_id = "codex-desktop"
callback_url = "http://127.0.0.1/callback"
```

`context:read` permits the request-reading tool; `context:publish` permits publication. Discovery includes protected-resource metadata and an RFC 8414 authorization-server document; 401 and insufficient-scope responses identify metadata/scopes. Browser consent displays the client and scopes after normal email verification. It uses a nonce CSP, no framing, same-origin consent checks, memory-only app credentials, and explicit Allow/Deny. The temporary sign-in refresh credential is revoked after consent. Authorization responses include `iss` and the client's state.

Pending consent expires in 10 minutes, authorization codes in 120 seconds, and opaque access tokens after `COPILOT_PERSONAL_AI_OAUTH_TOKEN_TTL_S` (default 1,200 seconds; allowed 60–3,600). There are no OAuth refresh tokens; reauthorize when expired. Codes are single-use via atomic compare/delete after PKCE/client/redirect/resource verification. Only token hashes are stored in Valkey. Issuance is limited to 20 active grants per account with expired-reference cleanup; authorization/consent/token requests also have per-peer minute limits. Proxies should preserve intended peer isolation when configuring deployment rate limits.

Context from my AI → Connected AI clients lists account-owned grants and disconnects them immediately. Disconnecting does not remove previously published briefs. The RFC 7009-style revocation endpoint also accepts the opaque token and matching public client ID. Token expiry, client removal, account deletion, wrong audience and revocation fail closed. Loss of Valkey state invalidates these short-lived grants.

Official references checked 2026-11-19: [Codex MCP authentication and callbacks](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [OpenAI plugin authentication](https://developers.openai.com/plugins/build/auth), and [MCP authorization 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## Real local AI-host evidence

Run `.venv/Scripts/python.exe scripts/validate_personal_ai_host.py` with the local Docker stack and an already authenticated installed Codex CLI. This opt-in script creates a temporary fully migrated database and fictional account, serves the actual API on a random loopback port, and supplies only the fixture's scoped OAuth token through a child-process environment variable. It uses ephemeral CLI configuration, does not inspect credential files, and does not persist an MCP registration. It drops the database on exit.

The 2026-11-19 run passed with Codex CLI 0.152.1: real `get_context_request` and `publish_context_brief` calls published version 1; application retrieval returned that private brief; refresh revision 2 led to a second real-host publication; removal made future retrieval empty and detail unavailable. The durable redacted summary is [personal-ai-host-2026-11-19.json](evidence/personal-ai-host-2026-11-19.json). Raw redacted tool evidence and result JSON are in ignored `target/personal-ai-host/{publish,refresh}.jsonl` and `result.json`. The initial ordinary fixture-bearer run also passed before the OAuth-token run. No external source research, real personal data, ChatGPT web connection or production email delivery is claimed.

The focused personal-AI/OAuth backend run passed 35 tests. Global Ruff and Pyright passed. Focused backend validation includes `test_personal_ai_oauth_stack.py`: discovery, same-origin consent, deny, malformed redirect/PKCE/scope/resource, code expiry/reuse/concurrent redemption, tool scopes, ordinary-API rejection, hash-only storage, account isolation/deletion, grant capacity/cleanup, access expiry, client removal, wrong audience and both revocation paths. Independent security review found no blocking bypass. Six desktop PersonalAI tests cover import/correction/removal, grant disconnection, stale grant responses and close-during-load.
