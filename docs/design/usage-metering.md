# Connected live usage and billing preparation

Implemented as an additive application change on the existing deployment shape.
Payment collection is not enabled. The displayed amounts are usage estimates.

## What counts

The server measures the time a host is connected to a live session after the ready/replay
messages have been sent. Silence counts. Startup before readiness, reconnect gaps, background
generation after disconnect/end, guest connections, and `no_call` sessions do not count.
Pausing guest sharing is not a pause of the host's live service. Concurrent host sessions
in different workspaces each contribute their own connected time to the payer's total.

Each attachment uses a monotonic clock anchored to UTC. Same-process reconnects keep a
consistent UTC mapping even if the system clock changes. It emits a stable-ID checkpoint
every `COPILOT_USAGE_INTERVAL_S` (10 seconds by default), and the remaining confirmed time
on disconnect, normal completion, replacement, or orderly shutdown. A new attachment starts
a new clock. A process crash can lose its unconfirmed tail; recovery never invents that time
or charges the outage interval. Abrupt network loss is recognized when the socket detects
it; this is server-observed connection time, not proof that a human was actively listening.

The configured session-log flush interval determines when checkpoints reach PostgreSQL
(currently 30 seconds by default). The usage screen reports committed checkpoints and can
lag the active call. This is not a per-audio-frame database write path.
At the default interval, an uninterrupted connected hour produces about 360 checkpoints.
Production storage retention, partitioning, and reporting rollups should be sized from
measured call hours and query load before increasing to the proposed user count.

## Durable records

Migration `0029_usage_ledger.sql` adds:

- `usage_prices`: immutable versions of currency and hourly minor-unit rates.
- `live_usage_sessions`: the authenticated payer, workspace and pinned price for a session.
- `live_usage_events`: the input checkpoints and stable idempotency keys.
- `live_usage_intervals`: accepted, non-overlapping time segments split at UTC midnight.
- `usage_adjustments`: append-only signed monetary corrections with a reason.
- `legacy_usage_estimates`: unpriced snapshots of historical wall-clock totals.

New durable admission pins attribution before the first interval; the worker validates it
when accepting checkpoints. The ledger retains the
distinction between the paying account and the workspace whose context was used. Another
account cannot resume the host connection and silently spend the first account's balance;
it must start a new session. Guest access remains separate from billing attribution.

An event and all its accepted segments commit atomically. Session-level locking serializes
writers; subtracting already-covered time prevents duplicate charges even when overlapping
checkpoints have different IDs. Reusing an event ID with changed contents is rejected.
Durations and money arithmetic use integers; displayed currency totals are rounded once
per reporting currency, not once per checkpoint.

These accounting records have no cascading foreign key to retained interview history.
Deleting a transcript/session therefore cannot change measured usage. Content-free accounting
records are retained independently of account/workspace deletion; there are no transcripts,
resumes, email addresses, or tokens in the ledger. A production accounting-retention policy
and controlled purge process are separate from the interview-history deletion workflow.

Runtime API roles can read only their authenticated payer's measured usage and cannot write
usage or adjustments. Workers append records under the workspace scope. A trusted correction
helper exists; no public payment or adjustment endpoint is exposed.

## Pricing configuration

The gateway defaults to a **tracking estimate of USD $5/hour**:

```text
COPILOT_BILLING_PRICE_VERSION=live-usd-v1
COPILOT_BILLING_CURRENCY=USD
COPILOT_BILLING_RATE_MINOR_PER_HOUR=500
COPILOT_USAGE_INTERVAL_S=10
```

Change the price version whenever changing its rate/currency. Existing sessions resume at
their original rate. The database rejects reuse of a price version with a different price.
The estimate does not include taxes, prepaid pack allocation, payment fees, or subscription
discounts. No balance is debited and no payment provider is called.

`GET /v1/usage?month=YYYY-MM` reports payer-scoped measured time, daily totals, price/currency
estimates, and the last recorded checkpoint from one consistent database snapshot. Responses
are not cached. The existing combined
`live_seconds` / `live_minutes` fields remain for older clients; new clients distinguish
measured connected time from nonbillable historical estimates. The desktop history screen
labels the new estimate as tracking only, with no charges, and continues refreshing visible
usage after a call ends so the worker's final checkpoint can appear.

## Migration and deployment

Completed historical sessions are snapshotted once as nonbillable estimates. Older gateways
can still finalize sessions through the legacy snapshot trigger. An already-running legacy
session remains legacy when resumed; only a new session starts connected-time measurement.
Historical wall time is never converted into a charge at the new rate.

Use the coordinated cutover in [production data foundation](production-data-foundation.md).
Although the metering tables are additive, the combined ownership migration changes RLS
and write contracts: drain calls and stop old writers before applying the full chain.
Rehearse against a backup using the normal migration runner; do not drop accounting
records or rewrite applied migration files.
No production migrations or infrastructure changes are performed by this implementation.

## Related reliability changes

Retained gateway logs and host send queues have entry and byte limits. Retention overflow
ends service explicitly instead of silently evicting accepted history. Slow host consumers
are disconnected resumably. Active-session claim/replacement is atomic, and new connections
are rejected when ownership cannot be verified. Lease acquisition for resumes and expired
lease recovery check session state atomically, so ended/deleted state cannot be revived by
a delayed reconnect or renewal. A bounded append receipt makes retrying
a storage write safe when its acknowledgement was lost. Flushes query only the question IDs
needed by the batch, with child lookup indexes supplied by migration `0030`.
For measured sessions, history deletion waits for durable finalization, release of the gateway
lease, and an empty retained log. An ended socket alone does not prove that usage was saved.

The account/workspace migration, synchronous durable session admission, operations/outbox,
worker generation checks and reserved persistence capacity are now implemented alongside
metering. This does not certify 1,000 concurrent calls or provide high availability on one
VM. Production load, restore and infrastructure failover testing remain deployment gates.
The current Valkey-to-worker persistence path can lose uncommitted data during a host failure,
prolonged storage outage, or shutdown that exceeds its drain window. Only retained, committed
usage contributes to estimates; enabling actual collection
requires reconciling those failure cases and deciding service-failure credit policy.
