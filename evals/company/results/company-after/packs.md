# standup pack

Mode: standup
Company: Larkspur Commerce
Role: Senior Backend Engineer
## Prepared answers
Q: What did you work on yesterday?
- PR #893: Merged PR #893 (CHK-1290), bringing checkout p95 down from 840 ms to 310 ms in staging. [n:w31f]
- PSP failover RFC: Reviewed Priya's RFC on switching a payment to the backup PSP when the primary times out; left comments on retry ordering. [n:w018]
Q: What are you working on today?
- Staging grant: Plan to pair with Dana on the staging grant on 2026-10-20. [n:wfc3]
- Backfill script for existing reservations: Plan to start the backfill script for existing reservations on 2026-10-20 (CHK-1311). [n:w658]
Q: Is anything blocking you?
- Outbox in staging: Blocked because the relay's database role lacks a grant on the outbox table in staging Postgres; only SRE can change roles there, and Dana is needed to grant access. [n:w429]
Q: What's your update?
- PR #893: Merged PR #893 (CHK-1290), bringing checkout p95 down from 840 ms to 310 ms in staging. [n:w31f]
- PSP failover RFC: Reviewed Priya's RFC on switching a payment to the backup PSP when the primary times out; left comments on retry ordering. [n:w018]
- Staging grant: Plan to pair with Dana on the staging grant on 2026-10-20. [n:wfc3]
- Backfill script for existing reservations: Plan to start the backfill script for existing reservations on 2026-10-20 (CHK-1311). [n:w658]
- Outbox in staging: Blocked because the relay's database role lacks a grant on the outbox table in staging Postgres; only SRE can change roles there, and Dana is needed to grant access. [n:w429]
## Standup on 2026-10-20
Yesterday (2026-10-19):
- [n:w31f] PR #893: Merged PR #893 (CHK-1290), bringing checkout p95 down from 840 ms to 310 ms in staging. (done)
- [n:w018] PSP failover RFC: Reviewed Priya's RFC on switching a payment to the backup PSP when the primary times out; left comments on retry ordering. (done)
Today:
- [n:wfc3] Staging grant: Plan to pair with Dana on the staging grant on 2026-10-20. (planned)
- [n:w658] Backfill script for existing reservations: Plan to start the backfill script for existing reservations on 2026-10-20 (CHK-1311). (planned)
Blockers:
- [n:w429] Outbox in staging: Blocked because the relay's database role lacks a grant on the outbox table in staging Postgres; only SRE can change roles there, and Dana is needed to grant access. (blocked, 2026-10-19)
Open commitments:
- [n:w8e3] Send Marco the outbox rollout plan. (due 2026-10-23)
- [n:w099] Add a dedupe table for order events with a 7-day TTL. (due 2026-10-27)
Earlier this week:
- [n:w22f] Wire the relay into staging: Wire the outbox relay into staging on Monday, 2026-10-19. (planned, 2026-10-16)
- [n:w6ab] PR #893: Get PR #893 merged on Monday, 2026-10-19. (planned, 2026-10-16)
- [n:w5b4] On-call handoff: Handed off on-call to Dana with nothing open. (done, 2026-10-16)
- [n:w6e0] Outbox relay: Polling the outbox with SELECT ... FOR UPDATE SKIP LOCKED in batches of 100 and publishing to SQS FIFO with the reservation id as message group and dedupe id. (in progress, 2026-10-16)
- [n:wfed] 7-day TTL for order event deduplication: The duplicate events incident postmortem assigned Alex an action item to add a dedupe table for order events with a 7-day TTL. (2026-10-15)
- [n:w3d4] Outbox migration: Started CHK-1305 with the outbox table and relay skeleton in Go. (in progress, 2026-10-15)
- [n:wd29] Dedupe table for order events: Add a dedupe table with a 7-day TTL as a postmortem action item, due 2026-10-27. (planned, 2026-10-15)
- [n:w487] Postmortem for the 10-13 duplicate events incident: Postmortem identified an action item to add a dedupe table for order events with a 7-day TTL. (done, 2026-10-15)
- [n:w576] Keep SQS FIFO and build the outbox: Choose a transactional outbox over Kafka: SQS FIFO deduplication and idempotent warehouse consumers are sufficient; Kafka would add cost and on-call load. Revisit Kafka in Q1 if reservations exceed 2,000 messages per second. (2026-10-14)
- [n:wf6b] Keep SQS FIFO and add transactional outbox: At the reservation sync with Priya, Marco and Dana, decided to keep SQS FIFO for reservations and add the transactional outbox; revisit Kafka in Q1 if reservations exceed 2,000 messages a second. (2026-10-14)

# meeting pack

Mode: meeting
Company: Larkspur Commerce
Role: Senior Backend Engineer
## This meeting
Title: Reservation outbox design review
Agenda: Rollout and backfill of the reservation outbox; how we keep duplicates out of the warehouse and the ledger feed.
## Projects
- [n:w692] Reservation sync: Add a transactional outbox so reservation events reach SQS even if the reservation service crashes after writing to Postgres.. Stack: Postgres, SQS FIFO
## Systems and stack
- [n:w19d] payments adapter: Adapter that sends refunds to the PSP; retries after a timeout can send a refund twice.
- [n:w4cc] reservation service: Handles inventory reservations; events can be lost if it crashes between the DB write and the SQS send.
- [n:wd64] order-pipeline: Processes order events through a consumer group; a rebalance caused 312 duplicates in the downstream ledger feed.
- [n:w182] Ledger feed: Kafka topic that finance reads orders from; it must not contain duplicates.
- [n:w5ea] Fraud scoring client: Python library called by the Checkout API and owned by Sam Okafor; times out after 800 ms and fails open.
- [n:w17f] Checkout API: Python FastAPI service on ECS Fargate behind an ALB; owns cart, pricing and checkout flow and calls the payments adapter over gRPC.. Stack: Python, FastAPI, ECS Fargate, ALB, gRPC
## People
- [n:w781] Dana: Paired with Alex on the payments adapter's on-call dashboard.
- [n:w7ea] Priya (Reviewer): Reviewing PR #881 for PSP-412 (refund idempotency).
- [n:wb87] Marco: Planned contact for a sync on reservation work for Q4.
- [n:we33] Sam: Has a fraud scoring client timeout PR that Alex promised to review by Monday.
## Decisions
- [n:wfed] 7-day TTL for order event deduplication: The duplicate events incident postmortem assigned Alex an action item to add a dedupe table for order events with a 7-day TTL. (2026-10-15)
- [n:w576] Keep SQS FIFO and build the outbox: Choose a transactional outbox over Kafka: SQS FIFO deduplication and idempotent warehouse consumers are sufficient; Kafka would add cost and on-call load. Revisit Kafka in Q1 if reservations exceed 2,000 messages per second. (2026-10-14)
- [n:wf6b] Keep SQS FIFO and add transactional outbox: At the reservation sync with Priya, Marco and Dana, decided to keep SQS FIFO for reservations and add the transactional outbox; revisit Kafka in Q1 if reservations exceed 2,000 messages a second. (2026-10-14)
- [n:wf54] Consumer idempotency on order id: Made the consumer idempotent on order id to mitigate duplicate order events following a consumer group rebalance. (2026-10-13)
- [n:w7e6] Reservation outbox: The design writes the event to an outbox table in the same Postgres transaction as the reservation; a relay publishes it to SQS FIFO. (2026-10-12)
- [n:w483] Batch-load promo rules: Fix the cart pricing N+1 query in CHK-1290 by batch-loading promo rules per cart instead of issuing one SELECT per cart line. (2026-10-09)
- [n:w356] Refund idempotency: Use the refund id as the idempotency key, enforce uniqueness on (provider, idempotency_key) in the adapter's Postgres, and return the stored response on retry. (2026-10-07)
- [n:w4c1] Idempotency key per refund: Plan to store an idempotency key per refund with the response to prevent duplicate refunds when the adapter retries after a timeout. (2026-10-06)
- [n:w7bb] Postgres per service: Use a separate RDS Postgres database per service, with no shared databases.
- [n:w292] Fraud scoring timeout: The fraud scoring client times out after 800 ms and fails open.
## Terms used here
- [n:wd43] PSP failover: Switching a payment to the backup PSP when the primary times out.
- [n:w9e5] Reservation hold: Inventory held by a cart during checkout; expires after 15 minutes without payment.
- [n:w2ef] the adapter: The payments adapter, which connects to payment service providers.
- [n:w3ff] Auth-capture gap: Time between card authorization and payment capture; up to 7 days for back-ordered items.
- [n:wea6] transactional outbox: A reservation event written in the same database transaction as its reservation, then published by a relay.
- [n:wbff] PSP: Payment service provider, such as Stripe or Adyen.
- [n:wddd] reservations-dlq: Dead-letter queue for reservation messages that fail five receives.
- [n:w3dc] duplicate-tolerant consumer requirement: Warehouse consumers must handle duplicate reservation events.
- [n:wc87] dlq-replay: Tool Dana's team uses to replay the reservations dead-letter queue after a fix.
## Recent work
- [n:w429] Outbox in staging: Blocked because the relay's database role lacks a grant on the outbox table in staging Postgres; only SRE can change roles there, and Dana is needed to grant access. (blocked, 2026-10-19)
- [n:w018] PSP failover RFC: Reviewed Priya's RFC on switching a payment to the backup PSP when the primary times out; left comments on retry ordering. (done, 2026-10-19)
- [n:w31f] PR #893: Merged PR #893 (CHK-1290), bringing checkout p95 down from 840 ms to 310 ms in staging. (done, 2026-10-19)
- [n:w5b4] On-call handoff: Handed off on-call to Dana with nothing open. (done, 2026-10-16)
- [n:w6e0] Outbox relay: Polling the outbox with SELECT ... FOR UPDATE SKIP LOCKED in batches of 100 and publishing to SQS FIFO with the reservation id as message group and dedupe id. (in progress, 2026-10-16)
- [n:w3d4] Outbox migration: Started CHK-1305 with the outbox table and relay skeleton in Go. (in progress, 2026-10-15)
- [n:w487] Postmortem for the 10-13 duplicate events incident: Postmortem identified an action item to add a dedupe table for order events with a 7-day TTL. (done, 2026-10-15)
- [n:w572] PR #893: Opened PR #893 for CHK-1290 (batch-load promo rules); waiting on Dana's review. (blocked, 2026-10-14)
- [n:wc37] CHK-1290: Fix in progress. (in progress, 2026-10-13)
- [n:w512] Duplicate order events: Led the SEV-3 incident from 14:20 to 15:05 after an order-pipeline consumer group rebalance sent 312 duplicates to the downstream ledger feed; mitigated by making the consumer idempotent on order id. (done, 2026-10-13)
- [n:w250] PSP-412: Deployed to production at 10:00; no duplicate refunds since. (done, 2026-10-13)
- [n:w536] Reservation outbox design doc: Wrote the design doc: store the event in an outbox table in the same Postgres transaction as the reservation, then publish to SQS FIFO through a relay. (done, 2026-10-12)
- [n:w4b3] Sam's fraud scoring client timeout PR: Reviewed PR #887, left two comments and approved. (done, 2026-10-12)
- [n:wde4] PR #881: PSP-412 merged after Priya's review. (done, 2026-10-12)
- [n:w912] CHK-1290: Confirmed an N+1 query in cart pricing: one SELECT per cart line for promo rules; the fix is to batch-load the rules per cart. (in progress, 2026-10-09)
## Open commitments
- [n:w8e3] Send Marco the outbox rollout plan. (due 2026-10-23)
- [n:w099] Add a dedupe table for order events with a 7-day TTL. (due 2026-10-27)