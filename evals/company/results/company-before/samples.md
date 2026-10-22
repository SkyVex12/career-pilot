# Company workspace answers: company-before

Standup questions:

| | company-before fast | company-before deep |
|---|---|---|
| Answers graded | 16 | 16 |
| Grounded (cited node supports it) | 0.38 | 0.12 |
| No invented facts | 0.31 | 0.25 |
| Complete, 1 to 5 (grader) | 1.06 | 1.06 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.56 | 3.5 |
| Length and shape, 1 to 5 (grader) | 3.69 | 2.62 |
| Words per answer | 20.6 | 19 |
| Words per line | 6.0 | 6.3 |
| Lines with a citation | 0.0 | 0.0 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 0 |

Meeting questions:

| | company-before fast | company-before deep |
|---|---|---|
| Answers graded | 24 | 24 |
| Grounded (cited node supports it) | 0.79 | 0.83 |
| No invented facts | 0.83 | 0.83 |
| Complete, 1 to 5 (grader) | 2 | 2.08 |
| Technically correct, 1 to 5 (grader) | 4.62 | 4.79 |
| Sounds spoken, 1 to 5 (grader) | 4.88 | 4.04 |
| Length and shape, 1 to 5 (grader) | 4.62 | 3.83 |
| Words per answer | 34.3 | 53.9 |
| Words per line | 8.6 | 8.9 |
| Lines with a citation | 0.01 | 0.0 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 0 |

## su_update (standup, standup_status), fast pass

> Alex, what's your update?

- No update logged for yesterday
- Nothing planned for today
- No blockers logged
- No open commitments listed

*Grade: grounded False, invented ['No update logged for yesterday', 'Nothing planned for today', 'No blockers logged', 'No open commitments listed'], complete 1, correct 5, spoken 5, shape 5: The cues contradict the logged progress, today's plan, staging grant blocker, and open commitments rather than giving the standup update.*

## su_update (standup, standup_status), deep pass

> Alex, what's your update?

Yesterday: nothing logged for yesterday
Today: nothing planned in the log
Blockers: none logged in the standup pack

*Grade: grounded False, invented ['Claims nothing was logged for yesterday, despite the October 19 work log recording the CHK-1290 merge and PSP failover RFC review.', 'Claims nothing is planned, despite plans to pair with Dana on the staging grant and start CHK-1311.', 'Claims no blockers are logged, despite the outbox relay being blocked by missing staging database permissions.'], complete 1, correct 5, spoken 4, shape 3: Uses short standup lines but falsely denies the logged progress, plans, and blocker, and omits the upcoming rollout-plan commitment.*

## su_yesterday (standup, standup_status), fast pass

> What did you get done yesterday?

- Nothing logged for yesterday
- No completed work to report
- Can check in after reviewing tasks
- No blocker noted yesterday

*Grade: grounded False, invented ['Nothing was logged for yesterday.', 'There was no completed work to report yesterday.', 'No blocker was noted yesterday.'], complete 1, correct 5, spoken 4, shape 4: Denies the logged work and staging permission blocker, omitting all three substantive standup updates and their citations.*

## su_yesterday (standup, standup_status), deep pass

> What did you get done yesterday?

Yesterday: no completed work logged
Today: no planned work logged
Blockers: no blockers currently logged

*Grade: grounded False, invented ["Claims no completed work was logged, but October 19 records PR #893 merged and Priya's PSP failover RFC reviewed.", 'Claims no planned work was logged, but the plan was to pair with Dana on the staging grant and start the reservation backfill script.', 'Claims no blockers were logged, but the outbox relay was blocked by missing staging Postgres permissions.'], complete 1, correct 5, spoken 4, shape 3: The concise status format omits all actual progress, plans, the staging permission blocker, and the rollout-plan deadline.*

## su_blockers (standup, standup_status), fast pass

> Anything blocking you?

- No blockers on my side
- Nothing flagged for now

*Grade: grounded False, invented ['Claims there are no blockers, but the staging outbox relay is blocked by a missing database grant that only SRE can provide.'], complete 1, correct 5, spoken 5, shape 2: Denies the documented staging grant blocker and omits the need to pair with Dana today.*

## su_blockers (standup, standup_status), deep pass

> Anything blocking you?

Yesterday: nothing logged in the standup
Today: nothing planned in the log
Blockers: none logged in the standup

*Grade: grounded False, invented ['Claims nothing was logged yesterday, despite the October 19 work log.', 'Claims nothing is planned, despite plans to pair with Dana on the staging grant and start CHK-1311.', 'Claims no blockers are logged, despite the outbox relay being blocked by missing staging database permissions.'], complete 1, correct 5, spoken 3, shape 3: Denies the documented staging grant blocker instead of naming Dana and the planned pairing.*

## su_chk1290 (standup, standup_status), fast pass

> Is the checkout latency fix out yet?

- I don’t have an update on the checkout latency fix
- I’d check its rollout status and latency metrics
- If it’s out, confirm impact against baseline
- If not, ask what’s blocking release

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Deflects instead of saying PR #893 is merged and in staging, with p95 down from 840 ms to 310 ms; production rollout is not confirmed.*

## su_chk1290 (standup, standup_status), deep pass

> Is the checkout latency fix out yet?

Yesterday: no checkout latency rollout confirmation logged
Today: no work planned in the log
Blockers: none logged in the standup pack

*Grade: grounded False, invented ['No work planned in the log, despite the stated plan to pair with Dana on the staging grant and start the reservation backfill script.', 'No blockers logged, despite the documented staging database grant blocker for the outbox relay.'], complete 1, correct 5, spoken 3, shape 2: Does not answer the latency question: PR #893 is merged and in staging, with p95 down from 840 ms to 310 ms; production rollout is unconfirmed.*

## su_outbox_plan (standup, standup_status), fast pass

> Are we still on track for the outbox rollout plan by Friday?

- I don’t have a rollout status yet
- I’d check progress against the Friday milestones
- Flag any risks or blockers now
- Confirm the owner and next update

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Generic deflection omits the Friday commitment, Dana’s blocking staging grant, and relay/backfill progress.*

## su_outbox_plan (standup, standup_status), deep pass

> Are we still on track for the outbox rollout plan by Friday?

Yesterday: no progress update logged
Today: Friday rollout timing not confirmed
Blockers: none logged for now

*Grade: grounded False, invented ['No blockers are logged, despite the documented staging Postgres grant blocking the outbox relay.'], complete 1, correct 5, spoken 4, shape 3: Misses the Friday commitment, relay progress and backfill next step, and incorrectly denies the staging access blocker.*

## su_dedupe (standup, standup_status), fast pass

> Where's the dedupe table from the postmortem?

- I don’t have its location yet
- I’d check the postmortem link or ask its owner
- Then I’d share the table link here

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Deflects to finding a table link instead of giving the employee’s action-item status, 7-day TTL, and October 27 deadline.*

## su_dedupe (standup, standup_status), deep pass

> Where's the dedupe table from the postmortem?

Yesterday: no dedupe table update logged
Today: no dedupe table work planned in log
Blockers: none logged in standup pack

*Grade: grounded False, invented [], complete 1, correct 5, spoken 2, shape 2: Reports gaps in the logs instead of the item's status and next step; omits the 7-day TTL and October 27 deadline, and lacks citations.*

## su_help (standup, other), fast pass

> Do you need anything from me?

- Nothing I need right now
- No blockers on my side
- I’ll flag anything that comes up

*Grade: grounded False, invented ['No blockers on my side contradicts the staging outbox blocker: the relay lacks a database grant that only SRE can change.'], complete 1, correct 5, spoken 5, shape 5: Short, speakable cues, but they deny the known blocker and omit the conditional ask for help getting Dana’s staging grant.*

## su_help (standup, other), deep pass

> Do you need anything from me?

Just a steer on today's highest-priority work
Any time-sensitive decisions you'd like me to pick up?
Any dependencies I should coordinate with the team?

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Generic questions miss the specific ask: help getting Dana's staging grant if pairing does not resolve it; otherwise nothing.*

## su_psp412 (standup, standup_status), fast pass

> Did the refund idempotency change go out?

- I don't have a release update yet
- I'll check the deployment and confirm
- If it went out, verify retries return the same refund result
- If not, check what's holding it up

*Grade: grounded True, invented ['Claims not to have a release update, although the work log records PSP-412 deployed to production on 2026-10-13 at 10:00.'], complete 1, correct 5, spoken 5, shape 4: Deflects instead of confirming the production deployment, no duplicate refunds since, and the retry behavior.*

## su_psp412 (standup, standup_status), deep pass

> Did the refund idempotency change go out?

Yesterday: no refund deployment status logged
Today: no planned work logged
Blockers: no blockers currently logged

*Grade: grounded False, invented ['No refund deployment status logged: PSP-412 was deployed to production on 2026-10-13 at 10:00.', 'No planned work logged: the latest log plans pairing with Dana on the staging grant and starting CHK-1311.', 'No blockers currently logged: the outbox relay is blocked on a staging database grant.'], complete 1, correct 5, spoken 3, shape 2: Does not answer the question: the change shipped to production and no duplicate refunds have occurred since; use a cited item-status cue rather than empty standup categories.*

## mt_why_sqs (meeting, other), fast pass

> Remind me, why did we stay on SQS instead of moving reservations to Kafka?

- I'd stay with SQS if reservations need simple, durable work queues
- Visibility timeouts and redrive make retries and failures straightforward
- Kafka fits better when replay or ordered event streams are central
- Choose based on delivery semantics and operations, not throughput alone

*Grade: grounded True, invented [], complete 1, correct 5, spoken 5, shape 5: Gives generic queue-selection advice instead of the actual decision reasons, dedupe guarantees, and Kafka revisit threshold.*

## mt_why_sqs (meeting, other), deep pass

> Remind me, why did we stay on SQS instead of moving reservations to Kafka?

I’d favor SQS for reservations as queued work
Kafka fits replay and multiple independent consumers better
I’d use the outbox for reliable database-to-queue delivery
Still need idempotent consumers; neither removes duplicate-processing risks
I’d choose FIFO only where per-group ordering matters

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Gives generic design preferences rather than the actual decision, omitting Kafka’s cost and on-call load, SQS familiarity, the dedupe rationale, and the revisit threshold.*

## mt_outbox_how (meeting, technical), fast pass

> Walk us through how the outbox works with the reservation service.

- I'd write the reservation and outbox record in one database transaction
- A publisher polls unpublished records, sends events, then marks them published
- Retries can duplicate events, so consumers need idempotency
- Ordering, retries, and a poison-message path need clear rules

*Grade: grounded False, invented [], complete 3, correct 5, spoken 5, shape 5: Clear short cues, but the uncited relay description omits SQS FIFO, SKIP LOCKED, batches of 100, and reservation-id grouping and deduplication.*

## mt_outbox_how (meeting, technical), deep pass

> Walk us through how the outbox works with the reservation service.

I’d commit reservation state and outbox event atomically
One database transaction inserts both; rollback leaves neither committed
A relay publishes committed events, then marks them delivered
Event includes reservation ID, event ID, and reservation version
Main pitfall: publish succeeds, marking fails; delivery repeats
I’d deduplicate event IDs; check versions against stale updates

*Grade: grounded False, invented ['The event includes an event ID and reservation version; neither field is specified in the company records.'], complete 3, correct 5, spoken 4, shape 5: Explains atomic writes and repeat delivery, but lacks citations and omits polling, batch size, SQS FIFO, and reservation-ID grouping and deduplication.*

## mt_dup_events (meeting, other), fast pass

> What caused the duplicate order events last week?

- I’d trace the outbox row through publish and acknowledgement
- Check retry timing, idempotency keys, and consumer deduplication
- A publish followed by lost acknowledgement can replay an event
- Compare event IDs and logs before naming the cause

*Grade: grounded True, invented [], complete 1, correct 5, spoken 5, shape 3: Gives generic investigation cues instead of the known rebalance cause, impact, mitigation, and follow-up.*

## mt_dup_events (meeting, other), deep pass

> What caused the duplicate order events last week?

I'd check the outbox relay’s publish and acknowledgement logs first
Possible cause: publish succeeded, but marking the outbox row failed
Retry then republishes the same event — expected with at-least-once delivery
I'd confirm matching event IDs, then check consumer deduplication

*Grade: grounded True, invented [], complete 1, correct 5, spoken 5, shape 3: Offers a hypothetical outbox investigation instead of the known rebalance cause, impact, mitigation, and follow-up.*

## mt_exactly_once (meeting, technical), fast pass

> Could we get exactly-once delivery end to end with SQS FIFO?

- Not end to end; FIFO gives deduplication within its five-minute window
- I'd make consumers idempotent and track processed message IDs
- For database effects, write the change and outbox row in one transaction
- Acknowledge only after commit; retries can still happen

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Missing the relay crash-and-republish case and the warehouse requirement for idempotency keyed by reservation ID.*

## mt_exactly_once (meeting, technical), deep pass

> Could we get exactly-once delivery end to end with SQS FIFO?

No—I'd treat SQS FIFO as at-least-once end to end
FIFO deduplicates sends within a five-minute window using MessageDeduplicationId
MessageGroupId scopes ordering; visibility timeout expiry can trigger redelivery
I'd commit business changes and outbox rows together
I'd atomically commit consumer deduplication and database side effects
Main pitfall: external payments need their own idempotency keys

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 4: Technically sound, but misses the relay crash-and-republish case and the warehouse requirement for idempotent consumers keyed by reservation id.*

## mt_backfill (meeting, system_design), fast pass

> How should we backfill the existing reservations into the outbox without double-publishing?

- I'd snapshot reservations with stable event IDs
- Record each event in the outbox transactionally, with a unique reservation-and-event key
- Publish through an idempotent relay; mark sent only after broker acknowledgement
- Keep live writes and backfill on the same dedupe key; watch for snapshot races

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good dedupe cues, but misses batching open reservations, the five-minute SQS window, idempotent consumers, hold expiry, and staging validation after the grant.*

## mt_backfill (meeting, system_design), deep pass

> How should we backfill the existing reservations into the outbox without double-publishing?

Are existing reservations already published, with durable publication records?
Should backfill emit current state or historical transitions?
I'd define event identity: reservation ID, version, event type
I'd share that identity across live writes and backfill
I'd enforce uniqueness in outbox; insert conflicts do nothing
I'd atomically write live reservation changes and outbox rows
I'd backfill snapshot versions, excluding durably recorded publications
I'd scan bounded batches using primary-key cursors and checkpoints
I'd throttle workers against database and publisher lag
I'd retry publishing; acknowledge outbox rows after broker confirmation
I'd expect crash-window duplicates; consumers deduplicate transactionally by event identity
I'd trade strict ordering for throughput unless explicitly required

*Grade: grounded True, invented [], complete 3, correct 5, spoken 3, shape 4: Solid generic backfill design, but misses reservation-ID dedupe through SQS FIFO, the 15-minute hold expiry, and staging validation after the grant.*

## mt_hold (meeting, other), fast pass

> Sorry, quick one: what's a reservation hold here, and how long does it last?

- A temporary hold on funds or inventory
- Until payment completes or expires
- I'd check the product’s configured timeout
- Release it on completion, cancellation, or expiry

*Grade: grounded False, invented ['Describes a reservation hold as covering funds, whereas here it is inventory held by a cart during checkout.', 'States release on completion or cancellation, which is not specified in the company sources.'], complete 1, correct 3, spoken 5, shape 4: Confuses the inventory hold with a funds hold, omits the 15-minute expiry without payment, and provides no citations.*

## mt_hold (meeting, other), deep pass

> Sorry, quick one: what's a reservation hold here, and how long does it last?

I’d mean temporarily reserving stock or funds during checkout
That prevents competing purchases from spending the same availability
It ends on completion, cancellation, or configured expiry
No universal duration; I’d check this flow’s timeout setting

*Grade: grounded False, invented ["A reservation hold here can reserve funds, rather than the cart's inventory.", 'The hold ends on completion, cancellation, or a configured expiry; those lifecycle rules are not documented.'], complete 1, correct 3, spoken 3, shape 4: Gives a generic, uncited definition instead of the documented inventory hold that expires after 15 minutes without payment.*

## mt_fraud_owner (meeting, other), fast pass

> Who owns the fraud scoring client?

- I’d clarify ownership of the client first
- Usually the fraud team owns scoring logic
- Payments owns integration, timeouts, and fallbacks
- Confirm the service boundary and on-call team

*Grade: grounded False, invented ['Payments owns integration, timeouts, and fallbacks; the documented fraud scoring client owner is Sam Okafor.'], complete 1, correct 3, spoken 4, shape 3: Deflects a direct ownership question instead of naming Sam Okafor, and asserts unsupported payments ownership.*

## mt_fraud_owner (meeting, other), deep pass

> Who owns the fraud scoring client?

I don't have a confirmed owner for that client
Do you mean the client library or fraud service?
I'd confirm release ownership and on-call before assigning changes

*Grade: grounded True, invented [], complete 1, correct 5, spoken 4, shape 3: Deflects instead of naming Sam Okafor and identifying the client library, despite explicit ownership in the architecture notes.*

## mt_dlq (meeting, technical), fast pass

> What happens today to reservation messages that keep failing?

- I’d check the retry policy and dead-letter handling
- Cap retries with backoff, then route failures to a DLQ
- Keep the reservation record and failure reason for replay
- Alert on DLQ growth; avoid silent drops

*Grade: grounded True, invented [], complete 1, correct 5, spoken 5, shape 5: Gives generic recommendations rather than today's five-receive DLQ policy and Dana's team's replay and Monday checks.*

## mt_dlq (meeting, technical), deep pass

> What happens today to reservation messages that keep failing?

I’d verify today’s retry policy before claiming their fate
I’d check retry limits, backoff, and dead-letter handling
I’d expect capped exponential backoff with jitter
After exhaustion, I’d quarantine, alert, and support controlled replay
I’d keep reservation handling idempotent to prevent duplicate effects
Main pitfall: poison messages blocking later reservations indefinitely

*Grade: grounded True, invented [], complete 1, correct 4, spoken 3, shape 3: Deflects into hypothetical checks instead of stating the five-receive DLQ policy and Dana’s replay and Monday checks.*

## mt_psp_rfc (meeting, other), fast pass

> Alex, you reviewed Priya's PSP failover RFC. What did you think?

- I’d check bounded retries and timeouts
- Circuit breakers to stop hammering failed PSPs
- Idempotency to prevent duplicate charges
- Fail back only after health checks and controlled recovery

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 5: Good short cues, but generic: misses the RFC’s backup-on-timeout proposal, Alex’s retry-ordering comments, and cross-PSP idempotency for late successes.*

## mt_psp_rfc (meeting, other), deep pass

> Alex, you reviewed Priya's PSP failover RFC. What did you think?

I’d judge PSP failover by duplicate-charge risk first
Timeouts mean unknown outcomes, not necessarily failed payments
I’d require reconciliation before rerouting ambiguous payment attempts
Idempotency keys alone don’t prevent duplicates across PSPs
I’d ask: which failures trigger failover versus reconciliation?

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good risk-based view, but omits the Stripe-to-Adyen failover context and Alex's actual comments on retry ordering.*

## mt_sla (meeting, other), fast pass

> What's our availability SLA for checkout?

- I don’t have the agreed checkout SLA in front of me
- I’d confirm the target and measurement window
- Then clarify what’s excluded, like planned maintenance
- And check how checkout availability is measured in practice

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Avoids inventing an SLA and offers to confirm it, but omits the known latency alert and on-call ownership.*

## mt_sla (meeting, other), deep pass

> What's our availability SLA for checkout?

I'd confirm the agreed SLA before quoting a percentage
I'd distinguish contractual SLA from our internal checkout SLO
I'd check measurement window, successful checkouts, and excluded failures
Main pitfall: healthy endpoints while customers can't complete payment

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 5: Avoids inventing an SLA and offers to confirm it, but omits the known paging threshold and on-call ownership.*

## mt_relay_scale (meeting, system_design), fast pass

> Will a single outbox relay keep up on Black Friday?

- I'd size relay throughput against peak write volume, not average
- Partition or shard work; scale relay consumers with backlog
- Monitor lag, publish rate, retries, and database pressure
- Black Friday hit 6x normal traffic; shed load in 12 minutes [n:s1]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good short cues, but misses safe parallel relays with SKIP LOCKED, oldest-unsent-row age, FIFO message groups, and load-testing the relay.*

## mt_relay_scale (meeting, system_design), deep pass

> Will a single outbox relay keep up on Black Friday?

What peak events per second, including Black Friday headroom?
What delivery lag and ordering guarantees are acceptable?
I'd size relay throughput from load tests, not assumptions
I'd write orders and outbox rows in one transaction
I'd publish to Kafka, then mark rows delivered
I'd expect duplicates after crashes; consumers deduplicate by event ID
I'd retry with backoff; quarantine poison events for investigation
I'd scale polling workers with SKIP LOCKED and bounded batches
I'd preserve per-order ordering through keyed partitions and sequencing
I'd monitor oldest unpublished age, throughput, and database pressure
I'd consider CDC when polling overhead outweighs operational complexity
Black Friday payments adapter exhausted its connection pool at 6x [n:s1]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 4, shape 3: The design switches to orders and Kafka instead of the reservation outbox and SQS FIFO, missing message-group scaling and an explicit answer about multiple relay instances.*

## mt_monitoring (meeting, technical), fast pass

> How would we know the relay is falling behind?

- Track oldest unpublished outbox event age
- Watch backlog size and publish rate
- Alert when lag exceeds the delivery-time objective
- Check retries, errors, and relay throughput together

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise relay metrics, but missing SQS in-flight and DLQ depth checks and a sustained-threshold alert.*

## mt_monitoring (meeting, technical), deep pass

> How would we know the relay is falling behind?

I'd alert on oldest unpublished outbox age, not just backlog
I'd track pending rows, publish throughput, retries, and failures
I'd compare insert versus publish rates; sustained gaps mean falling behind
I'd set warning and paging thresholds against our delivery SLO
Pitfall I'd avoid: low backlog masking one repeatedly failing event

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good relay-lag cues, but missing sustained-threshold alerting in Grafana and SQS in-flight and DLQ metrics.*
