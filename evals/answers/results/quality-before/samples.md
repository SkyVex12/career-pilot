# Answer samples: quality-before

All questions:

| | quality-before fast | quality-before deep |
|---|---|---|
| Answers graded | 52 | 52 |
| Grounded (cited node supports it) | 0.88 | 0.98 |
| No invented facts | 0.92 | 0.98 |
| Complete, 1 to 5 (grader) | 4.04 | 4.31 |
| Technically correct, 1 to 5 (grader) | 4.88 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.9 | 4.94 |
| Length and shape, 1 to 5 (grader) | 4.77 | 4.87 |
| Words per answer | 32.2 | 42.5 |
| Words per line | 8.5 | 8.6 |
| Lines with a citation | 0.35 | 0.42 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.02 | 0 |
| Full written sentences per answer | 0 | 1.37 |

Questions the profile does not cover:

| | quality-before fast | quality-before deep |
|---|---|---|
| Answers graded | 34 | 34 |
| Grounded (cited node supports it) | 0.85 | 0.97 |
| No invented facts | 0.88 | 0.97 |
| Complete, 1 to 5 (grader) | 3.85 | 4.09 |
| Technically correct, 1 to 5 (grader) | 4.82 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.85 | 4.91 |
| Length and shape, 1 to 5 (grader) | 4.85 | 4.82 |
| Words per answer | 33.7 | 42.1 |
| Words per line | 8.6 | 8.8 |
| Lines with a citation | 0.17 | 0.29 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.03 | 0 |
| Full written sentences per answer | 0 | 1.85 |

## background (behavioral), fast pass

> So to kick things off, can you walk me through your background and what you're working on right now?

- Eight years backend; senior on Larkspur’s checkout platform [n:a1]
- Own the order pipeline; moved reconciliation from batch to Kafka streaming [n:s2]
- Before that, tracking APIs and event ingestion at Tidewater [n:a2]
- Looking for bigger payments scale and more system design ownership

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## background (behavioral), deep pass

> So to kick things off, can you walk me through your background and what you're working on right now?

Senior Backend Engineer, Larkspur Commerce, Checkout Platform [n:a1]
Before that, tracking APIs and event ingestion at Tidewater [n:a2]
Now own the order pipeline and payments adapter layer [n:a1]
Order pipeline to Kafka: replaced nightly batch reconciliation [n:s2]
Kafka, Go consumers, PostgreSQL; side by side for 6 weeks [n:s2]
Reconciliation lag: 24 hours to under 2 minutes [n:s2]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident (behavioral), fast pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

- Black Friday: payments adapter exhausted connections at 6x normal traffic [n:s1]
- Led the incident; shed load with a feature flag in 12 minutes [n:s1]
- Revenue loss held under 0.3% of the day [n:s1]
- Added per-provider pool limits and load tests in CI [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident (behavioral), deep pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

Checkout outage on Black Friday [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Led the incident; shed load with a feature flag [n:s1]
Load shed in 12 minutes; revenue loss under 0.3% of the day [n:s1]
Then added pool limits per provider and load tests in CI [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident_followup (behavioral), fast pass

> And what did you change afterwards so it wouldn't happen again?

- Black Friday checkout outage: per-provider pool limits [n:s1]
- Load tests in CI after the incident [n:s1]
- Feature flag shed load in 12 minutes [n:s1]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Covers both preventive changes, but the third cue repeats incident mitigation rather than explaining how the changes prevent recurrence.*

## incident_followup (behavioral), deep pass

> And what did you change afterwards so it wouldn't happen again?

Checkout outage on Black Friday [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Added pool limits per provider to contain connection exhaustion [n:s1]
Added load tests in CI for traffic spikes [n:s1]
Revenue loss held under 0.3% of the day [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## rate_limiter (system_design), fast pass

> How would you design a rate limiter for a public API that has to work across several regions?

- Token bucket per API key, Redis with atomic Lua updates
- Key by API key and region; shared global quota needs coordinated state
- Local in-process buckets during Redis failover [n:s6]
- Set limits, refill rate, and fail-open or fail-closed by endpoint

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Clear cues, but the multi-region core is missing: explain how to enforce a global quota and trade off coordination latency against overshoot.*

## rate_limiter (system_design), deep pass

> How would you design a rate limiter for a public API that has to work across several regions?

Is the limit global per API key, or per region?
How much overshoot is acceptable, and what latency budget?
Components: gateway, regional Redis token buckets, global quota allocator.
Data flow: allocate regional quotas; atomically consume tokens; reject with Retry-After.
Scaling: shard by API key; rebalance regional quotas with demand.
Tradeoffs: regional quotas reduce latency; strict global limits need coordination.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise design, but missing how regional quotas and limiter behavior handle outages or network partitions.*

## schema_changes (technical), fast pass

> I'm curious how you handled schema changes in that pipeline

- Schema evolution: backward-compatible Avro schemas [n:s3]
- Schema registry, CI blocks breaking changes [n:s3]
- Zero consumer breakages across 140 changes in 18 months [n:s3]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## schema_changes (technical), deep pass

> I'm curious how you handled schema changes in that pipeline

Schema registry with backward-compatible Avro schemas [n:s3]
CI check blocks breaking changes before deployment [n:s3]
Keep new fields optional or defaulted for older records
Zero consumer breakages in 18 months, after 140 schema changes [n:s3]
Tradeoff: breaking changes need staged migrations, not immediate replacement

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## process_thread (technical, not covered), fast pass

> What's the difference between a process and a thread, and when would you pick one over the other?

- Processes: separate address spaces, stronger isolation, higher communication cost
- Threads: shared memory within a process, cheaper communication, need synchronization
- Pick processes for isolation or independent services
- Pick threads for concurrent work sharing state in one program

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## process_thread (technical, not covered), deep pass

> What's the difference between a process and a thread, and when would you pick one over the other?

Process: separate memory; thread: shared memory within a process.
OS schedules both; threads share resources, each keeping its stack.
Processes communicate through IPC; threads need synchronization around shared state.
Go consumers in order pipeline to Kafka used goroutines, not threads. [n:s2]
Pick processes for isolation; threads for cheaper shared-memory concurrency.
Tradeoff: process overhead versus thread races and shared failures.

*Grade: grounded False, invented ["The Kafka pipeline's Go consumers used goroutines, not threads; [n:s2] establishes Go consumers but not their concurrency implementation."], complete 5, correct 5, spoken 5, shape 5: Strong, concise comparison, but the cited story does not support the claimed use of goroutines.*

## do_differently (behavioral), fast pass

> What would you do differently if you built it again

- Shipment-ETA launch: more real-world validation first [n:s8]
- Rural routes: shadow mode before release [n:s8]
- Per-region accuracy dashboards before relaunch [n:s8]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 2: The cues switch to the ETA launch instead of addressing what to change when rebuilding the pipeline discussed in the call.*

## do_differently (behavioral), deep pass

> What would you do differently if you built it again

Order pipeline to Kafka [n:s2]
Replaced nightly batch reconciliation with a streaming pipeline [n:s2]
Ran old and new side by side for 6 weeks [n:s2]
Reconciliation lag: 24 hours to under 2 minutes [n:s2]
Build again: backward-compatible Avro schemas from the start [n:s3]
Add the CI check that blocks breaking changes earlier [n:s3]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear, grounded cues, but the proposed changes would be stronger with a brief explanation of why introducing them earlier matters.*

## index (technical, not covered), fast pass

> How does a database index actually speed up a query?

- B-tree index, jump to matching keys instead of scanning every row
- Narrow row IDs first, then fetch the matching rows
- Extra storage and slower writes; most useful for selective filters
- Composite indexes help when query filters match the leftmost columns

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## index (technical, not covered), deep pass

> How does a database index actually speed up a query?

An index finds matching rows without scanning the whole table.
Usually a B-tree: sorted keys, pointing to matching rows.
Traverse the tree, then fetch only the needed rows.
Tracking service: two indexes and a cache; p95 down 70%. [n:s4]
Tradeoff: extra storage and slower writes to maintain indexes.

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## manager (behavioral), fast pass

> Have you ever disagreed with a decision your manager made? What happened?

- Tracking-service rewrite disagreement: manager wanted a new language [n:s4]
- Proposed a two-week spike with measurable criteria [n:s4]
- Database bottleneck; two indexes and a cache, p95 down 70% [n:s4]
- Manager agreed, and we dropped the rewrite [n:s4]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## manager (behavioral), deep pass

> Have you ever disagreed with a decision your manager made? What happened?

Disagreed with manager on a rewrite [n:s4]
Manager wanted tracking service rewritten in a new language [n:s4]
Proposed a two-week spike with measurable criteria instead [n:s4]
Database bottleneck; fixed with two indexes and a cache [n:s4]
p95 down 70%; manager agreed, rewrite dropped [n:s4]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## url (technical, not covered), fast pass

> Walk me through what happens when you type a URL into the browser and hit enter.

- Browser parses URL, checks cache, resolves host through DNS
- Opens TCP connection; TLS handshake for HTTPS
- Sends HTTP request; server processes it and returns a response
- Browser parses HTML, fetches assets, builds page and renders it

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## url (technical, not covered), deep pass

> Walk me through what happens when you type a URL into the browser and hit enter.

Browser turns the URL into a request, then renders the response
DNS resolves hostname; TCP connects, TLS encrypts for HTTPS
HTTP request reaches server; HTML returns, browser fetches assets and renders
Checkout backend example: order pipeline and payments adapter layer [n:a1]
Caching cuts latency, but stale content needs invalidation

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good concise sequence, but the work example merely names backend responsibilities rather than connecting them to the request flow.*

## leaving (behavioral), fast pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

- Roadmap shifting to frontend; want to stay deep in backend
- Payments at larger scale, like your streaming settlement work [n:s2]
- More system design ownership

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## leaving (behavioral), deep pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

Current role — Larkspur Commerce, Checkout Platform [n:a1]
Our roadmap is shifting to frontend work
Moved order reconciliation from nightly batch to Kafka streaming [n:s2]
Lag went from 24 hours to under 2 minutes [n:s2]
Looking for larger-scale payments and more system design ownership
Your batch-to-streaming settlement work fits that direction

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## questions_for_us (other), fast pass

> We're nearly out of time. Do you have any questions for us?

- How far along is the batch-to-streaming move, and what’s hardest so far?
- How do squads split ownership of shared payment services?
- What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## questions_for_us (other), deep pass

> We're nearly out of time. Do you have any questions for us?

How far along is the batch-to-streaming move?
What is hardest so far?
How do squads split ownership of shared payment services?
What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## idempotency (technical, not covered), fast pass

> How would you make a payment API idempotent, so a retried request never charges the customer twice?

- Idempotency key per payment request; scope it to the customer
- Store key, request hash, and payment result durably
- Atomically claim the key before charging; duplicates return the stored result
- Keep the claim through retries and timeouts; reconcile uncertain outcomes before retrying the charge

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise cues, but missing provider-side idempotency, rejection of same-key requests with different bodies, and a retention window.*

## idempotency (technical, not covered), deep pass

> How would you make a payment API idempotent, so a retried request never charges the customer twice?

Require an idempotency key for each logical payment.
Atomically claim merchant-plus-key; store request hash and payment status.
Reject changed payloads; retries return saved results or pending.
Reuse that key with provider; reconcile timeouts before retrying.
My payments adapter layer: same boundary to protect. [n:a1]
Tradeoff: durable state and reconciliation; guarantees depend on provider support.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise design, but omits a retention window and leaves durable storage of the original response implicit.*

## k8s_probes (technical, not covered), fast pass

> What's the difference between a liveness probe and a readiness probe in Kubernetes?

- Liveness: is the process stuck or dead
- Failed liveness check, Kubernetes restarts the container
- Readiness: can this pod handle traffic now
- Failed readiness check, pod stays running but leaves service endpoints

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear core distinction, but omits dependency-check pitfalls and startup probes for slow starts.*

## k8s_probes (technical, not covered), deep pass

> What's the difference between a liveness probe and a readiness probe in Kubernetes?

Liveness: restart unhealthy containers; readiness: stop routing traffic.
Repeated liveness failures trigger container restarts, after configured thresholds.
Readiness failures remove Pods from Service endpoints; no restart.
For my payments adapter: readiness could flag exhausted connections. [n:s1]
Tradeoff: dependency-based liveness risks restart loops during downstream outages.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear and well-shaped; add that liveness targets stuck processes and startup probes protect slow starts.*

## isolation (technical, not covered), fast pass

> Can you walk me through transaction isolation levels and what each one protects you from?

- **Read uncommitted** — may see uncommitted writes; dirty reads
- **Read committed** — committed data only; dirty reads prevented, but non-repeatable reads can happen
- **Repeatable read** — same row reads stay consistent; phantom rows may still appear, depending on the database
- **Serializable** — transactions behave as if run one at a time; prevents anomalies, with more blocking or retries

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear fast-pass overview, but omits write skew or lost updates and PostgreSQL's default and snapshot-isolation behavior.*

## isolation (technical, not covered), deep pass

> Can you walk me through transaction isolation levels and what each one protects you from?

Read uncommitted: dirty reads possible; read committed: prevents dirty reads.
Repeatable read: also prevents nonrepeatable reads; phantoms still possible.
Serializable: prevents phantoms and serialization anomalies; equivalent to serial execution.
Implemented through locks, snapshots, and conflict checks; database behavior varies.
Order pipeline: PostgreSQL; isolation choice depends on reconciliation invariants. [n:s2]
Tradeoff: stronger isolation can mean blocking or transaction retries.

*Grade: grounded True, invented [], complete 3, correct 5, spoken 4, shape 5: Clear standard-level overview, but omits write skew or lost updates and PostgreSQL’s read-committed default and snapshot-based repeatable read.*

## exactly_once (technical, not covered), fast pass

> Kafka says it supports exactly-once. What does that actually mean, and where does it break down?

- Kafka transactions: atomic reads and writes within Kafka, including consumed offsets
- `read_committed` consumers skip aborted transactional records
- Outside Kafka, side effects like database writes aren’t covered; retries can duplicate them
- Use idempotency or an outbox pattern for end-to-end effects

*Grade: grounded True, invented [], complete 3, correct 3, spoken 5, shape 5: Good boundary and side-effect cues, but transactions make writes and offset commits atomic—not reads—and the answer omits producer idempotence, fencing, and performance costs.*

## exactly_once (technical, not covered), deep pass

> Kafka says it supports exactly-once. What does that actually mean, and where does it break down?

Exactly-once: committed effects once, within Kafka’s transactional boundary
Idempotent producers deduplicate retries; transactions commit output and offsets together
Consumers use read_committed to hide aborted or uncommitted records
Order pipeline to Kafka: Go consumers, PostgreSQL reconciliation [n:s2]
PostgreSQL writes or payment calls: need separate deduplication, not Kafka guarantees
Tradeoff: transaction overhead, added latency; retries and reprocessing still happen

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise answer, but omits cross-partition atomicity and fencing zombie producers with transactional IDs.*

## ledger (system_design, not covered), fast pass

> How would you design a ledger service for a payments platform that never loses or double-counts money?

- Immutable, balanced double-entry transactions; never update posted entries
- Idempotency keys and unique constraints prevent duplicate postings
- PostgreSQL transaction for atomic debit and credit; outbox to Kafka
- Reconcile ledger against payment and settlement records; alert on mismatches

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong fast-pass cues; add account-level concurrency control and clarify requirements as the answer develops.*

## ledger (system_design, not covered), deep pass

> How would you design a ledger service for a payments platform that never loses or double-counts money?

Single currency or multiple currencies, including foreign exchange?
Expected throughput, and must balances be immediately consistent?
Components: PostgreSQL, immutable double-entry journal, unique idempotency keys, transactional outbox.
Flow: atomically deduplicate, validate balanced entries, lock balances, append, enqueue.
Scaling: partition by account; cross-partition transfers need coordination, not independent writes.
Tradeoffs: correctness over availability; durable replication, reconciliation, reversals instead of edits.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong design cues, but clarify how balances derive from journal entries and explicitly reconcile against external providers.*

## notifications (system_design, not covered), fast pass

> Design a system that sends email and push notifications to fifty million users.

- Durable notification queue; user and channel preferences; dedupe by notification ID
- Partition Kafka by user ID; consumers batch and scale out
- Separate email and push providers; rate limits, retries with backoff, dead-letter queue
- Track delivery status; idempotency keys and per-channel metrics

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong architecture cues, but missing clarification of send volume, latency, and transactional versus marketing needs.*

## notifications (system_design, not covered), deep pass

> Design a system that sends email and push notifications to fifty million users.

Fifty million users total, or notifications within what delivery window?
Transactional or marketing; what ordering, consent, and delivery guarantees?
Components: API, scheduler, preference store, queues, email and push workers.
Data flow: check consent, enqueue, deduplicate, send, record provider status.
Scaling: partition queues, autoscale workers, respect provider limits, retry with backoff.
Tradeoffs: at-least-once delivery risks duplicates; provider acceptance isn't confirmed delivery.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise design; add templates, explicit idempotency keys, and a dead-letter queue for exhausted retries.*

## learn_fast (behavioral, not covered), fast pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- Order pipeline to Kafka: learned Go consumers quickly [n:s2]
- Replaced nightly batch reconciliation with streaming
- Ran old and new side by side for six weeks
- Reconciliation lag: 24 hours to under 2 minutes [n:s2]

*Grade: grounded False, invented ['Learned Go consumers quickly for the Kafka pipeline project.'], complete 2, correct 5, spoken 5, shape 4: Short, usable cues, but they invent rapid learning and give migration results rather than explaining how the candidate learned under a deadline.*

## learn_fast (behavioral, not covered), deep pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

Order pipeline to Kafka [n:s2]
Nightly batch reconciliation; lag of 24 hours [n:s2]
Replaced batch with Kafka, Go consumers, PostgreSQL [n:s2]
Ran old and new side by side for 6 weeks [n:s2]
After cutover: reconciliation lag under 2 minutes [n:s2]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 3: Accurate Kafka story, but it neither acknowledges the loose fit nor explains learning quickly or handling a deadline.*

## bad_news (behavioral, not covered), fast pass

> Tell me about a time you had to deliver bad news to a customer or a stakeholder.

- Shipment-ETA launch: rural predictions off by hours [n:s8]
- Rolled it back; shared the issue with stakeholders
- Added shadow mode and per-region accuracy dashboards
- Relaunched five weeks later, 92% within 30 minutes [n:s8]

*Grade: grounded False, invented ['Shared the shipment-ETA issue with stakeholders; no such communication is documented.'], complete 3, correct 5, spoken 5, shape 5: The cues invent stakeholder communication and omit how to tell early, explain impact, and set the next update; the middle factual bullets also lack citations.*

## bad_news (behavioral, not covered), deep pass

> Tell me about a time you had to deliver bad news to a customer or a stakeholder.

Failed launch: shipment-ETA feature [n:s8]
Shipped without enough real-world validation [n:s8]
Rural predictions off by hours; rolled back [n:s8]
Added shadow mode and per-region accuracy dashboards [n:s8]
Relaunched 5 weeks later; 92% within 30 minutes [n:s8]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 4: Accurate, concise failed-launch cues, but they omit delivering the bad news: early disclosure, impact, recovery actions, and the next update.*

## time_zones (behavioral, not covered), fast pass

> How have you handled working with a team spread across several time zones?

- I don’t have a specific time-zone story in my notes
- Agree on clear owners and handoffs
- Keep decisions and updates written down
- Use overlap time for discussions; async for routine work

*Grade: grounded True, invented [], complete 5, correct 5, spoken 4, shape 5: Strong honest approach, though referring to 'my notes' sounds like preparation commentary rather than a natural interview answer.*

## time_zones (behavioral, not covered), deep pass

> How have you handled working with a team spread across several time zones?

No specific multi-time-zone story to point to
My approach: written decisions, clear owners, explicit handoffs
Shared overlap for blockers and live discussions
Rotate meeting times; track delays between handoffs

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## why_company (other, not covered), fast pass

> Why do you want to work at Northwind Payments specifically?

- Batch-to-streaming payments work, like my Kafka reconciliation project [n:s2]
- Bigger payments scale and more backend system design ownership
- Go, PostgreSQL, and Kafka match my platform experience [n:a1]

*Grade: grounded False, invented [], complete 5, correct 5, spoken 5, shape 5: The stack match is supported by the skills section and [n:s2], but the cited [n:a1] does not establish those tools.*

## why_company (other, not covered), deep pass

> Why do you want to work at Northwind Payments specifically?

Your batch-to-streaming move matches my order pipeline to Kafka work [n:s2]
Reconciliation lag: 24 hours to under 2 minutes [n:s2]
Looking for larger-scale payments and more system design ownership
Current team's roadmap shifting to frontend work

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong fit and goals, though mentioning Northwind's matching stack would make the company-specific connection fuller.*

## salary (other, not covered), fast pass

> What are your salary expectations for this role?

- No salary range in my notes
- Could you share the range budgeted for the role?
- I’d compare it with the scope and total compensation

*Grade: grounded True, invented [], complete 4, correct 5, spoken 3, shape 4: The first cue exposes the notes rather than answering naturally, and flexibility for the right scope is only implied.*

## salary (other, not covered), deep pass

> What are your salary expectations for this role?

Looking at the full package, not just base salary
What range have you budgeted for this role?
Happy to discuss within that range, depending on scope

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## culture (other, not covered), fast pass

> What kind of team culture do you do your best work in?

- Clear ownership across the team
- Open debate, decisions based on measurable tests [n:s4]
- Teams keep autonomy with shared schema checks [n:s3]
- Calm incident reviews and useful runbooks [n:s5]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong, grounded cues; written design and explicitly blameless incident reviews would round out the working-style answer.*

## culture (other, not covered), deep pass

> What kind of team culture do you do your best work in?

Clear ownership, room for system design decisions
Disagreements settled through measurable experiments, not opinions [n:s4]
Shared learning through mentoring and incident runbooks [n:s5]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong, grounded cues on ownership, evidence and shared learning; blameless reviews and written design are not explicit.*

## five_years (other, not covered), fast pass

> Where do you see yourself in five years?

- Senior backend, bigger-scale payments platform role [n:a1]
- More system design ownership
- Deep in backend, not frontend work
- Helping move batch settlement to streaming [n:s2]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 4: Clear short cues, but they describe the next role more than five-year growth and omit mentoring others and owning reliability.*

## five_years (other, not covered), deep pass

> Where do you see yourself in five years?

More system design ownership on a larger-scale payments platform
Staying deep in backend, close to the technical decisions
Helping other engineers grow, building on mentoring three juniors [n:s5]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong, concise goals and mentoring cues, but misses owning reliability.*

## first_unique (coding, not covered), fast pass

> How would you find the first non-repeating character in a string?

- Count each character in one pass
- Scan again, return the first count of one
- Hash map: O(n) time, O(k) space
- No unique character: return none

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong fast-pass algorithm cues; empty input, Unicode, and case handling are not explicit.*

## first_unique (coding, not covered), deep pass

> How would you find the first non-repeating character in a string?

Approach: count characters, then scan for the first count-one character
Complexity: O(n) time; O(k) space for distinct characters
Edge cases: empty string, all repeating; return agreed sentinel

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear two-pass solution, but omits Unicode handling and case sensitivity.*
