# Company workspace answers: company-after

Standup questions:

| | company-after fast | company-after deep |
|---|---|---|
| Answers graded | 16 | 16 |
| Grounded (cited node supports it) | 0.81 | 1.0 |
| No invented facts | 0.94 | 1.0 |
| Complete, 1 to 5 (grader) | 3.62 | 4 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.81 | 4.88 |
| Length and shape, 1 to 5 (grader) | 4.38 | 4.69 |
| Words per answer | 27.4 | 26.9 |
| Words per line | 8.3 | 8.6 |
| Lines with a citation | 0.92 | 1.0 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 0 |

Meeting questions:

| | company-after fast | company-after deep |
|---|---|---|
| Answers graded | 24 | 24 |
| Grounded (cited node supports it) | 1.0 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Complete, 1 to 5 (grader) | 3.54 | 3.83 |
| Technically correct, 1 to 5 (grader) | 5 | 4.88 |
| Sounds spoken, 1 to 5 (grader) | 5 | 4.62 |
| Length and shape, 1 to 5 (grader) | 4.92 | 4.71 |
| Words per answer | 27.1 | 54.4 |
| Words per line | 7.7 | 8.4 |
| Lines with a citation | 0.64 | 0.48 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.12 | 0 |
| Full written sentences per answer | 0 | 0 |

## su_update (standup, standup_status), fast pass

> Alex, what's your update?

- Merged PR #893 (CHK-1290), checkout p95 840 ms to 310 ms in staging [n:w31f]
- Reviewed Priya’s PSP failover RFC; comments on retry ordering [n:w018]
- Today: pair with Dana on the staging grant; start CHK-1311 backfill [n:wfc3] [n:w658]
- Blocked: relay role needs outbox grant in staging; Dana needed, only SRE can change roles [n:w429]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## su_update (standup, standup_status), deep pass

> Alex, what's your update?

Yesterday: merged PR #893; staging p95 840→310 ms [n:w31f]
Reviewed Priya’s PSP failover RFC; comments on retry ordering [n:w018]
Today: pair with Dana on staging grant [n:wfc3]
Start CHK-1311 backfill script for existing reservations [n:w658]
Blocked: relay’s database role lacks staging outbox grant; Dana needed [n:w429]
Due 2026-10-23: send Marco the outbox rollout plan [n:w8e3]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## su_yesterday (standup, standup_status), fast pass

> What did you get done yesterday?

- Merged PR #893 (CHK-1290): checkout p95 840 ms to 310 ms in staging [n:w31f]
- Reviewed Priya’s PSP failover RFC; comments on retry ordering [n:w018]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 3: Missing the outbox staging permission blocker and a third talking point.*

## su_yesterday (standup, standup_status), deep pass

> What did you get done yesterday?

Yesterday: merged PR #893; reviewed Priya's PSP failover RFC, retry-ordering comments [n:w31f] [n:w018]
Today: pair with Dana on staging grant; start CHK-1311 backfill [n:wfc3] [n:w658]
Blocked: relay role lacks staging outbox grant; needs Dana from SRE [n:w429]
Due 2026-10-23: send Marco the outbox rollout plan [n:w8e3]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good standup cues, but yesterday's update omits the checkout p95 improvement from 840 ms to 310 ms in staging.*

## su_blockers (standup, standup_status), fast pass

> Anything blocking you?

- Relay's database role lacks outbox-table grant in staging Postgres [n:w429]
- Only SRE can change staging roles [n:w429]
- Dana needed to grant access [n:w429]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Names the blocker and needed owner clearly, but omits the plan to pair with Dana today.*

## su_blockers (standup, standup_status), deep pass

> Anything blocking you?

Outbox blocked: relay’s database role lacks staging Postgres outbox grant [n:w429]
Only SRE can change roles; Dana needed to grant access [n:w429]
Plan today: pair with Dana on the staging grant [n:wfc3]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## su_chk1290 (standup, standup_status), fast pass

> Is the checkout latency fix out yet?

- PR #893 merged; checkout p95 310 ms in staging [n:w31f]
- Not out in production yet
- Next, pair with Dana on staging grant [n:wfc3][n:w429]

*Grade: grounded False, invented ['The claim that the fix is not out in production yet is not established by the logs; production rollout is simply undocumented.'], complete 4, correct 5, spoken 5, shape 4: Clearly reports merged and staging, but asserts an unknown production status and adds an unrelated outbox grant next step.*

## su_chk1290 (standup, standup_status), deep pass

> Is the checkout latency fix out yet?

Merged PR #893 (CHK-1290); checkout p95 840→310 ms in staging [n:w31f]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Accurate staging status, but missing a next step for the production rollout.*

## su_outbox_plan (standup, standup_status), fast pass

> Are we still on track for the outbox rollout plan by Friday?

- Outbox rollout plan due Friday, October 23 [n:w8e3]
- Need Dana’s staging grant; relay role lacks outbox access [n:w429]
- I’d confirm rollout scope after the grant lands
- I’ll send Marco the plan by Friday [n:wbc3]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Missing relay progress, today's backfill work, and an explicit warning that Friday is at risk unless Dana grants access today.*

## su_outbox_plan (standup, standup_status), deep pass

> Are we still on track for the outbox rollout plan by Friday?

Outbox rollout plan for Marco due Friday, 2026-10-23 [n:w8e3]
Staging blocked: relay’s database role lacks outbox table grant [n:w429]
Today: plan to pair with Dana on staging grant [n:wfc3]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Missing relay progress, today's backfill work, and an explicit warning that Friday is at risk unless Dana unblocks staging today.*

## su_dedupe (standup, standup_status), fast pass

> Where's the dedupe table from the postmortem?

- Planned postmortem action item, 7-day TTL [n:wd29]
- Due 2026-10-27 [n:wd29]
- No progress update in today’s log
- Outbox grant is blocked on staging access [n:w429]

*Grade: grounded False, invented [], complete 3, correct 5, spoken 4, shape 5: Includes the TTL and deadline, but should plainly say it has not started and comes after the outbox work; the log-status claim lacks a citation.*

## su_dedupe (standup, standup_status), deep pass

> Where's the dedupe table from the postmortem?

Dedupe table: planned, 7-day TTL for order events [n:wd29]
Next: add the table, due 2026-10-27 [n:w099]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear status and deadline, but omits that the dedupe work comes after the outbox work.*

## su_help (standup, other), fast pass

> Do you need anything from me?

- Dana’s grant for the outbox role in staging [n:w429]
- SRE needs to change the database role [n:w429]
- Pairing with Dana on the staging grant today [n:wfc3]
- Then I can move ahead with the backfill script [n:w658]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 4: Names the grant blocker but does not clearly ask for help if pairing with Dana fails, or say otherwise nothing is needed.*

## su_help (standup, other), deep pass

> Do you need anything from me?

Help getting Dana for the staging grant [n:wfc3]
Relay's database role lacks an outbox table grant [n:w429]
Only SRE can change roles in staging Postgres [n:w429]
Plan to pair with Dana today, 2026-10-20 [n:wfc3]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 5: Makes the request unconditional rather than asking for help only if pairing with Dana fails, and omits that nothing else is needed.*

## su_psp412 (standup, standup_status), fast pass

> Did the refund idempotency change go out?

- Yes—PSP-412 deployed to production at 10:00; no duplicate refunds since [n:w250]
- Used the refund id as the idempotency key [n:w356]
- Unique on provider and idempotency key; retries return stored response [n:w356]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear status and implementation cues, but omits PR #881, merge date, and deployment date.*

## su_psp412 (standup, standup_status), deep pass

> Did the refund idempotency change go out?

PSP-412 deployed to production 2026-10-13 at 10:00; no duplicate refunds since [n:w250]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Direct status answer, but omits the merge and refund-key/stored-response details.*

## mt_why_sqs (meeting, other), fast pass

> Remind me, why did we stay on SQS instead of moving reservations to Kafka?

- SQS FIFO deduplication plus idempotent warehouse consumers are sufficient [n:w576]
- Kafka would add cost and on-call load [n:w576]
- Revisit Kafka in Q1 above 2,000 messages per second [n:w576]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good short cues, but omit the five-minute dedupe window, team familiarity with SQS, and decision context.*

## mt_why_sqs (meeting, other), deep pass

> Remind me, why did we stay on SQS instead of moving reservations to Kafka?

SQS FIFO plus outbox; Kafka adds cost and on-call load [n:w576]
The outbox closes the reservation-write-to-publish crash gap [n:w7e6]
FIFO deduplication helps; warehouse consumers still need idempotency [n:w576]
I'd treat FIFO's five-minute dedupe window as insufficient alone
Revisit Kafka in Q1 above 2,000 reservation messages per second [n:w576]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good direct answer, but omits team familiarity with SQS and the specific Kafka infrastructure tradeoff.*

## mt_outbox_how (meeting, technical), fast pass

> Walk us through how the outbox works with the reservation service.

- Same Postgres transaction: reservation and outbox event [n:w7e6]
- Relay polls in batches of 100, `SKIP LOCKED` [n:w6e0]
- Publishes to SQS FIFO, reservation ID as group and dedupe ID [n:w6e0]
- Consumers still need duplicate handling; FIFO isn’t end-to-end exactly-once [n:w3dc]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good concise walkthrough, but misses marking rows sent after publish and explicitly explaining that the relay can publish twice.*

## mt_outbox_how (meeting, technical), deep pass

> Walk us through how the outbox works with the reservation service.

Same Postgres transaction: reservation plus outbox event [n:w7e6]
Relay polls using SELECT ... FOR UPDATE SKIP LOCKED, batches of 100 [n:w6e0]
Publishes to SQS FIFO, reservation id as message group [n:w6e0]
I'd mark sent after acknowledgement; crashes mean possible redelivery
Warehouse consumers must handle duplicate reservation events [n:w3dc]
I'd change dedupe id: unique event, not reservation
FIFO deduplication lasts five minutes; I'd persist consumer deduplication

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Explains the flow well, but proposes changing the dedupe ID without first stating that the current relay uses the reservation ID.*

## mt_dup_events (meeting, other), fast pass

> What caused the duplicate order events last week?

- Consumer-group rebalance in the order pipeline [n:w512]
- It sent 312 duplicates downstream to the ledger feed [n:w512] [n:wd64]
- Consumers now deduplicate by order id [n:wf54]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear cause, impact and mitigation cues, but omits the planned 7-day dedupe table follow-up.*

## mt_dup_events (meeting, other), deep pass

> What caused the duplicate order events last week?

Order-pipeline consumer group rebalance sent 312 duplicates downstream [n:w512]
Those duplicates reached the ledger feed [n:w512]
Mitigated by making the consumer idempotent on order id [n:w512]
Next: dedupe table with 7-day TTL, due 2026-10-27 [n:w099]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear cause, impact, mitigation and follow-up; omits the incident timing, severity and Alex's lead role.*

## mt_exactly_once (meeting, technical), fast pass

> Could we get exactly-once delivery end to end with SQS FIFO?

- Not end-to-end; FIFO dedupe has a five-minute window
- Outbox closes the DB-write-to-send loss gap [n:w7e6]
- Relay retries can still publish duplicates
- I'd dedupe by event ID in each consumer [n:w3dc]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise cues, but omit at-least-once delivery on failure and the agreed warehouse consumer requirement keyed by reservation id.*

## mt_exactly_once (meeting, technical), deep pass

> Could we get exactly-once delivery end to end with SQS FIFO?

No — I’d target exactly-once effects, not exactly-once delivery
FIFO deduplicates sends for five minutes; receives can repeat
I’d atomically record event IDs with warehouse writes
Our outbox transaction covers reservations, not SQS publishing [n:w7e6]
Relay uses reservation id as group and dedupe id [n:w6e0]
I’d use event IDs; reservation IDs could suppress distinct events
For duplicate-free ledger output, I’d require transactional consume-and-produce [n:w182]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 4: Missing the relay crash-and-republish explanation and explicit reservation-keyed warehouse idempotency; the ledger tangent distracts from the answer.*

## mt_backfill (meeting, system_design), fast pass

> How should we backfill the existing reservations into the outbox without double-publishing?

- Idempotent outbox insert keyed by reservation ID
- Backfill in small, resumable batches; skip existing keys
- Relay publishes with reservation ID as FIFO dedupe ID [n:w6e0]
- Warehouse dedupes too; FIFO alone isn’t enough [n:w3dc]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise core approach, but misses expired holds, the five-minute FIFO dedupe window, and staging validation after the grant.*

## mt_backfill (meeting, system_design), deep pass

> How should we backfill the existing reservations into the outbox without double-publishing?

Are we backfilling current state, or historical transitions?
Can we identify reservations already published before the outbox?

I'd keep Postgres → outbox → SQS FIFO [n:w7e6]
I'd establish a cutover watermark with transactional writers enabled
I'd backfill below it, using bounded, checkpointed batches
I'd enforce unique event keys; INSERT ON CONFLICT DO NOTHING
I'd distinguish snapshot events from subsequent reservation transitions
I'd keep reservation-id grouping, but use event-specific dedupe IDs
I'd publish only through the relay; retry ambiguous sends
I'd expect duplicates beyond FIFO's five-minute deduplication window
I'd atomically dedupe warehouse writes by event key
I'd block backfill ledger delivery without durable downstream deduplication

*Grade: grounded True, invented [], complete 3, correct 4, spoken 3, shape 4: Useful batching and retry plan, but changes the intended reservation-id dedupe scheme, omits hold expiry and staging validation, and introduces unrelated ledger delivery.*

## mt_hold (meeting, other), fast pass

> Sorry, quick one: what's a reservation hold here, and how long does it last?

- Inventory held by a cart during checkout [n:w9e5]
- Expires after 15 minutes without payment [n:w9e5]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## mt_hold (meeting, other), deep pass

> Sorry, quick one: what's a reservation hold here, and how long does it last?

Reservation hold: inventory held by a cart during checkout [n:w9e5]
Expires after 15 minutes without payment [n:w9e5]
I'd think of it as temporary inventory protection

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## mt_fraud_owner (meeting, other), fast pass

> Who owns the fraud scoring client?

- Sam Okafor owns the fraud scoring client [n:w5ea]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Correctly names the owner, but omits the client context, timeout, and fail-open behavior.*

## mt_fraud_owner (meeting, other), deep pass

> Who owns the fraud scoring client?

Sam Okafor owns the fraud scoring client [n:w5ea]
Python library called by the Checkout API [n:w5ea]
Times out after 800 ms and fails open [n:w5ea]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## mt_dlq (meeting, technical), fast pass

> What happens today to reservation messages that keep failing?

- After five receives, messages go to `reservations-dlq` [n:wddd]
- I’d alert on DLQ depth and age
- Fix the cause before using `dlq-replay` [n:wc87]
- Replay safely; warehouse consumers must tolerate duplicates [n:w3dc]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Misses Dana's team owning replays and checking the DLQ every Monday; proposed alerting displaces part of the current process.*

## mt_dlq (meeting, technical), deep pass

> What happens today to reservation messages that keep failing?

After five receives, failing messages go to reservations-dlq [n:wddd]
SQS counts receives, not just explicit processing failures
Unacknowledged messages become available again after the visibility timeout
Dana's team uses dlq-replay after a fix [n:wc87]
Warehouse consumers must handle duplicate reservation events [n:w3dc]
I'd flag replay duplicates: FIFO deduplication lasts only five minutes

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Missing that Dana's team checks the DLQ every Monday.*

## mt_psp_rfc (meeting, other), fast pass

> Alex, you reviewed Priya's PSP failover RFC. What did you think?

- Retry ordering matters when switching PSPs [n:w018]
- Avoid charging both providers after an ambiguous timeout
- I'd use the refund or payment idempotency key across retries
- Reconcile provider outcomes before replaying uncertain requests

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good concise cues, but explicitly name Stripe-to-Adyen failover and say the payment idempotency key must carry across providers.*

## mt_psp_rfc (meeting, other), deep pass

> Alex, you reviewed Priya's PSP failover RFC. What did you think?

I’d prioritize avoiding duplicate payments over immediate failover
A timeout leaves payment outcome unknown, not necessarily failed
I reviewed Priya’s RFC; left comments on retry ordering [n:w018]
I’d reconcile primary status before trying the backup PSP
Can we walk through the unknown-outcome case together?

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Clear, practical view, but omits the RFC’s failover behavior and the need to preserve PSP-412 idempotency across PSPs.*

## mt_sla (meeting, other), fast pass

> What's our availability SLA for checkout?

- I don’t have a checkout availability SLA to quote
- I’d confirm the target with the service owner
- Then check the SLO and how it’s measured
- Separate checkout availability from dependency failures

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Avoids inventing an SLA and offers to confirm it, but omits the known latency alert and on-call ownership.*

## mt_sla (meeting, other), deep pass

> What's our availability SLA for checkout?

I'd confirm the agreed SLA before quoting a number
I'd distinguish contractual SLA from our internal availability SLO
I'd measure successful checkouts, not just API uptime
I'd count payment failures separately from valid declines
Are we asking contractual commitment or engineering target?

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 5: Offers to confirm rather than inventing an SLA, but omits the known paging threshold and on-call ownership in favor of generic measurement advice.*

## mt_relay_scale (meeting, system_design), fast pass

> Will a single outbox relay keep up on Black Friday?

- I’d size it from peak publish rate and backlog drain target
- Current relay polls 100 rows with `SKIP LOCKED` [n:w6e0]
- Measure throughput, queue age, DB load; scale relay workers if needed
- Keep FIFO ordering per reservation id [n:w6e0]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise cues, but misses safe multi-instance polling, oldest-unsent-row scaling, FIFO group throughput limits, and the Black Friday load-test lesson.*

## mt_relay_scale (meeting, system_design), deep pass

> Will a single outbox relay keep up on Black Friday?

What peak events per second, and acceptable publish lag?
How skewed is traffic toward individual reservations?
I'd start with Postgres → outbox relay → SQS FIFO [n:w7e6]
I'd benchmark batches of 100 with realistic SQS latency [n:w6e0]
I'd require headroom above forecast, not assume one suffices
I'd scale relay workers using FOR UPDATE SKIP LOCKED
I'd preserve per-reservation ordering across concurrent workers
I'd mark published after acknowledgement; crashes can still duplicate
I'd use event IDs, not reservation IDs, for deduplication
I'd require consumer idempotency beyond FIFO's five-minute window
I'd monitor oldest unpublished age, throughput, and database contention
I'd revisit Kafka above 2,000 messages per second [n:w576]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 4, shape 4: Strong design cues, but omit the Black Friday 6x pool-exhaustion lesson and do not explicitly connect reservation-ID message groups to FIFO throughput.*

## mt_monitoring (meeting, technical), fast pass

> How would we know the relay is falling behind?

- Track oldest unpublished event age and outbox row count [n:w7e6]
- Alert on age against the delivery-latency target
- Watch publish rate, errors, retries, and SQS queue depth
- I'd check backlog growth against reservation event volume and relay throughput

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good monitoring cues, but specify unsent-row count, sustained alert thresholds, SQS in-flight messages, and DLQ depth.*

## mt_monitoring (meeting, technical), deep pass

> How would we know the relay is falling behind?

I’d alert on oldest unpublished event age and backlog growth
I’d measure age from commit, not latest retry
I’d compare publish rate against incoming event rate
I’d track publish failures, retries, and database lock waits
Our relay polls batches of 100 using SKIP LOCKED [n:w6e0]
I’d set age thresholds against agreed delivery latency targets
Pitfall: low queue depth can hide an outbox backlog

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good relay-lag cues, but misses sustained-threshold alerting, Grafana, and SQS in-flight and DLQ metrics.*
