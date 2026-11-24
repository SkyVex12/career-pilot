# Account and workspace ownership transition

Migration `0031_explicit_ownership.sql` separates the signed-in account from the workspace selected by the client. UUIDs, ciphertext associated data, object keys, and existing content references stay stable. The API, gateway profile resolver, sign-in flows, and deletion worker use the new model.

| Authority | Purpose |
| --- | --- |
| `accounts` | Sign-in principal, account preferences, lifecycle status and private ownership. |
| `account_identities` | Unique `(provider, realm, subject)` identity mapped to an account. Existing providers use realm `default`; lookup and its cache include the realm. |
| `workspaces` | Shared career/work content, independently retained from its creator. `created_by_account_id` is attribution, with `ON DELETE SET NULL`. |
| `workspace_memberships` | Account access with `owner`, `editor`, or `viewer` role, active/revoked status, and authorization revision. |
| `external_workspace_links` | Unique `(provider, realm, external_id)` directory mapping to a workspace. |
| `account_keys` | Encryption keys for account-private integrations, Ask, Practice, calendars and personal context. |
| `account_tombstones` | Content-free retired account UUIDs; deleted IDs cannot reclaim retained workspaces or historical usage. |
| `user_keys` | Existing workspace encryption keys; canonical `workspace_id` makes their scope explicit. |

An account receives a personal workspace with the same UUID for compatibility. These are independent rows and independent ownership scopes. Creating a career workspace does not create a fake sign-in account. Multiple accounts can join the same workspace, including a personal workspace. A workspace's creator is never an ownership or deletion shortcut.

Account UUIDs are never reused after deletion. Known-identity tokens referring to a removed account are rejected. A freshly verified Google sign-in whose historical deterministic fallback UUID was retired receives a fresh UUID, leaving any retained shared workspace and historical usage attribution untouched. Account bootstrap also refuses to claim an independently existing workspace. Email uniqueness belongs to accounts only; a retained workspace's historical metadata cannot prevent its deleted creator from registering again.

Transactions carry both `app.account_id` (authenticated actor) and `app.user_id` (the selected legacy SQL scope). Account-private RLS uses the actor. Shared domain RLS requires an active membership in an active workspace and an active account, in addition to the selected scope. Viewers can read; insert/update/delete require owner or editor. Guest seats retain their narrow explicit guest access. Private session descendants inherit the current session visibility.

The profile resolver reads memberships through `app.account_workspaces()`. Career-OS discovery calls `app.provision_external_workspace()` only after checking remote access. Existing revoked memberships remain revoked, even when discovery runs again. `app.set_workspace_membership()` is the narrow owner-authorized database operation for grants, role changes, and revocation. API roles cannot directly mutate the membership table. There is no new membership-management UI in this change.

Every active workspace must retain an active owner account. Membership changes update and lock the workspace row, then deferred constraints verify the final transaction state. This permits ownership transfer in one transaction and prevents two owners concurrently removing the last ownership. Account deactivation must transfer ownership or archive/delete its otherwise ownerless workspaces first.

Private foreign keys point to accounts. Shared foreign keys point to workspaces. Existing composite references continue to enforce same-workspace relationships. Profile metadata now references a real workspace; a personal-context active version must belong to its exact request and account.

Account deletion first marks the account as deleting, revokes its memberships, transfers retained shared workspaces to another active member, and cancels outstanding work from that actor. Workspaces without another active member are marked deleting before content/object/key cleanup. The account row is deleted last, cascading private data and private keys. Operation and usage accounting records retain content-free identifiers under their independent retention policies. A retained shared personal workspace keeps its UUID, shared keys, source content, and objects after its original account is gone.

Private encryption now uses `account_keys` and a separate in-process key-cache namespace. Migration copies historical wrapped account keys with their existing versions, so historical ciphertext still decrypts. Newly created private keys are independent of workspace keys. Historical copies retain the same underlying key material until a deliberate re-encryption/rotation project; this migration does not rewrite ciphertext. Deleting a workspace cannot destroy a surviving account's integration credentials or private keys.

Compatibility names `users`, `identities`, and `career_os_people` are security-invoker views. `profiles` is retained as external listing metadata, not authorization authority; the migration backfills historical grants once, and subsequent profile inserts/deletes do not grant or revoke membership. Existing domain `user_id` input columns retain their SQL names, with explicit generated `workspace_id` or `account_id` ownership columns. New ownership code uses canonical tables. PostgreSQL `ON CONFLICT` writes must target the canonical tables, since the compatibility views cannot support that clause.

Apply this change with coordinated application binaries and migrations during the current internal-testing phase. Older binaries that insert/upsert `users` or derive authorization from profiles alone are not a supported mixed-version rollout. Migration tests cover a populated legacy schema, role isolation, identity realms, key lifetimes, viewer restrictions, revocation, and concurrent owner removal. Check the deployed image tag and migration ledger for each environment's rollout status.
