# Mobile/server compatibility contract

`GET /v1/client-capabilities` uses existing bearer-account authentication and returns a small, noncached manifest. It needs no selected profile or Career-OS link. Its handler performs no feature probe, provider request, storage mutation or background work. Existing account authentication remains unchanged.

```json
{
  "schema_version": 1,
  "server_version": "0.0.0",
  "contracts": {
    "companion": 1,
    "ask": 1,
    "practice": 1,
    "preparation": 1,
    "languages": 1
  }
}
```

`server_version` comes from the package version and is informational; the repository currently uses `0.0.0`. Compatibility is determined by the manifest schema and contract revisions, not by comparing that version string. Revision 1 is the first advertised contract. Revisions may advance for backward-compatible additions; a breaking manifest interpretation needs a new schema version.

| Contract | Revision 1 describes actual API behavior |
| --- | --- |
| `companion` | Profile-authorized active session listing/attachment; retained session `spoken_language` is a string or null. |
| `ask` | Profile-scoped conversations, message generation state, cancellation/retry, and authorized source resolution. |
| `practice` | Session state/actions, context preview, and explicit per-session `voice_available`/`speech_available` fields. |
| `preparation` | Consented calendar meeting preparation and editable private meeting context. |
| `languages` | Configured `/call-languages` catalog with language ID, display/native names and text direction. |

These are implemented API contracts. They do not assert that a proxy deployment is current, calendar OAuth is configured, model credentials work, an account has a particular entitlement, every catalog language is enabled, or speech/device quality is certified. Actual feature requests and capability fields remain authoritative. In particular, `languages: 1` does not assert that a deployment has enabled the entire current language catalog. The manifest also does not certify native assistant media, request-bound control approval or the distinct mobile host voice identity; those feature responses remain authoritative.

`apps/mobile/src/clientCapabilities.ts` provides a pure parser and a post-sign-in check integrated into `App.tsx`. `checkClientCapabilities(api, signal)` makes one authenticated request with automatic endpoint retry disabled; a deadline of at most four seconds includes token lookup and response parsing. The request is not polled. Account/server changes abort the check and clear its prior result. Cancellation and the account-generation guard prevent a stale result from replacing the current account's state.

The account panel displays the returned nonnull `notice`; a compatible result adds no message. A 404 gives an older-server upgrade notice; missing/older contract revisions identify the affected features. Malformed responses, 403/503 and timeouts remain unverified, never success. A 401 requests sign-in again and a 426 requests an app update. The compatibility message does not block features already implemented by an older server; individual feature requests must still surface their actual errors.

Focused validation covers authenticated routing and no handler writes, registered feature routes, malformed/missing contracts, authentication/unavailable errors, one-request behavior, the hard four-second ceiling, transports that ignore cancellation, and stale account cancellation.
