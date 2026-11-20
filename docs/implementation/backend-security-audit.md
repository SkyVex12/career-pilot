# Backend security and lifecycle audit — 2026-11-19

Scope: calendar preparation, Central Ask, private AI context/MCP, mobile companion, practice, and account-bound call/language integration. No external provider/model charges were required. Tests use real local PostgreSQL, Valkey and object-store services where marked `stack`, with scripted providers elsewhere.

## Corrections

- Private AI activation now rejects a conflicting live sticky account marker, verifies that the database actually accepted the sticky owner, and uses an atomic Valkey comparison of account/profile/status/context before returning briefs. Missing, ended, transferred, or scope-changed sessions are never recreated or overwritten. The database marker remains sticky after context deletion.
- Gateway private-context responses are discarded if the requesting account/profile/scope changed during the HTTP request or the task no longer owns the capture lease. This prevents an in-flight response for one account from being injected after another account resumes the shared profile.
- Migration `0026_context_reference_guard.sql` constrains an active brief to the same request and authorizing account. The existing employer/application references already correctly match `profile_id` to source `user_id`.
- RLS coverage now includes every new account-owned table, explicit role privileges, and exact account/profile foreign-key mappings. Tests reject a brief from another account, another request under the same account, and source references from another profile.
- Calendar connections in an error or reauthorization state no longer contribute planned events to preparation or new Ask retrieval. A delayed sync failure or declined OAuth callback cannot overwrite newer authorization state. Disconnect still deletes local tokens/cache/context when remote revocation fails.
- Citation resolution rejects inactive/superseded memory facts, preventing them from returning as practice evidence. Practice re-resolves source records on each generated turn; removed private brief versions and deleted sources fail closed. Prior answers retain their disclosed historical excerpts until their conversation/practice is deleted.
- Companion discovery and grant checks exclude `mobile_in_person` and `mobile-` clients, preserving the desktop-source label. Same-account grants remain separate from capture ownership and cannot bypass the sticky private-context marker.

## Validation

- Expanded targeted audit suite: 586 passed.
- Calendar race/companion follow-up suite: 14 passed.
- Gateway private-context/account-transfer and companion suite: 14 passed.
- Ruff and Pyright clean for changed production modules.
- First full run: 1,505 passed; failures identified missing schema audit inventory and an inaccessible default pytest temporary directory.
- Second full run using workspace temporary/cache directories: 1,678 passed; one calendar test had a local import shadowing error introduced during test extension. That test was corrected and passed in both targeted reruns.
- Final clean full suite: **1,684 passed in 383.57 seconds**, with evidence in `.implementation/backend-audit-clean.txt` and `.implementation/backend-audit-clean.xml`. The final gateway account-transfer guard was also validated in the separate 14-test private-context/companion run.

## Authentication boundaries

Existing signed access-token expiry and refresh-token revocation rules remain authoritative. Companion grant revocation is immediate on its next feed check and does not end capture. Mobile sign-out removes local credentials and revokes display grants through the account endpoint. Calendar OAuth secrets and refresh tokens remain backend-only, with encrypted persistence and one-use PKCE state; no credential-bearing QR or URL was added.

Real Google/Microsoft consent, signed mobile-device background behavior, and external provider quality/cost acceptance remain separate release validations. This audit does not claim those were exercised.
