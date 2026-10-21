# Answer samples: quality-after

All questions:

| | quality-after fast | quality-after deep |
|---|---|---|
| Answers graded | 52 | 52 |
| Grounded (cited node supports it) | 0.96 | 1.0 |
| No invented facts | 0.98 | 1.0 |
| Complete, 1 to 5 (grader) | 4.06 | 4.67 |
| Technically correct, 1 to 5 (grader) | 4.94 | 5 |
| Sounds spoken, 1 to 5 (grader) | 5 | 4.94 |
| Length and shape, 1 to 5 (grader) | 4.79 | 4.98 |
| Words per answer | 35.4 | 55.7 |
| Words per line | 8.9 | 8.8 |
| Lines with a citation | 0.23 | 0.25 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0.02 |
| Full written sentences per answer | 0 | 0 |

Questions the profile does not cover:

| | quality-after fast | quality-after deep |
|---|---|---|
| Answers graded | 34 | 34 |
| Grounded (cited node supports it) | 1.0 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Complete, 1 to 5 (grader) | 3.88 | 4.53 |
| Technically correct, 1 to 5 (grader) | 4.91 | 5 |
| Sounds spoken, 1 to 5 (grader) | 5 | 4.91 |
| Length and shape, 1 to 5 (grader) | 4.85 | 4.97 |
| Words per answer | 35.8 | 58.3 |
| Words per line | 9.0 | 9.0 |
| Lines with a citation | 0.07 | 0.07 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0.03 |
| Full written sentences per answer | 0 | 0 |

## background (behavioral), fast pass

> So to kick things off, can you walk me through your background and what you're working on right now?

- Eight years backend; now senior on Larkspur’s checkout platform [n:a1]
- Own the order pipeline; moved reconciliation from batch to Kafka streaming [n:s2]
- Before that, tracking APIs and event ingestion at Tidewater [n:a2]
- Looking for bigger payments scale and more system design ownership

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## background (behavioral), deep pass

> So to kick things off, can you walk me through your background and what you're working on right now?

Senior Backend Engineer, Larkspur Commerce, 2022 to now [n:a1]
Checkout Platform, 9 engineers; order pipeline and payments adapter layer [n:a1]
Order pipeline to Kafka: replaced nightly batch reconciliation [n:s2]
Kafka, Go consumers, PostgreSQL; 24 hours to under 2 minutes [n:s2]
Old and new side by side for 6 weeks [n:s2]
Previously Tidewater Logistics: shipment tracking APIs and event ingestion [n:a2]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident (behavioral), fast pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

- Black Friday payments outage: pool exhausted at 6x traffic [n:s1]
- Led the incident; shed load with a feature flag in 12 minutes [n:s1]
- Revenue loss held under 0.3% of the day [n:s1]
- Added per-provider pool limits and load tests in CI [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident (behavioral), deep pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

Checkout outage on Black Friday [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Led the incident; shed load with a feature flag in 12 minutes [n:s1]
Revenue loss held under 0.3% of the day [n:s1]
Then added pool limits per provider and load tests in CI [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident_followup (behavioral), fast pass

> And what did you change afterwards so it wouldn't happen again?

- Added per-provider connection pool limits [n:s1]
- Put load tests into CI [n:s1]
- I’d also track pool saturation and alert before exhaustion
- I’d test limits against peak traffic and provider failures

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## incident_followup (behavioral), deep pass

> And what did you change afterwards so it wouldn't happen again?

Checkout outage on Black Friday [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Afterwards, added pool limits per provider [n:s1]
Added load tests in CI [n:s1]
Revenue loss held under 0.3% of the day [n:s1]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## rate_limiter (system_design), fast pass

> How would you design a rate limiter for a public API that has to work across several regions?

- I’d use a distributed token bucket per API key, with explicit burst and refill limits
- Keep regional counters in Redis or a fast shared store; partition keys to avoid hot spots
- For global limits, coordinate quota across regions; trade strict accuracy for latency and availability
- Fail open or use local buckets during store outages; cap fallback traffic and monitor drift [n:s6]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good fast-pass cues; specify how global quota is divided or coordinated across regions to make the central design choice concrete.*

## rate_limiter (system_design), deep pass

> How would you design a rate limiter for a public API that has to work across several regions?

Are limits per API key, IP, or tenant?
How strict globally, and what latency budget?
I'd use gateway checks with per-key token buckets
I'd allocate regional quotas from a global coordinator
I'd keep summed allocations within the global limit
I'd check regional Redis buckets atomically using Lua
I'd return 429 with Retry-After when tokens run out
I'd shard by key; batch quota leases regionally
I'd expire leases before reallocating, preventing double spending
I'd bound local fallback; fail closed for sensitive endpoints
I'd trade cross-region coordination for temporarily stranded quota
Previously: Redis Lua, local fallback, 15k requests per second [n:s6]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## schema_changes (technical), fast pass

> I'm curious how you handled schema changes in that pipeline

- I used a schema registry with backward-compatible Avro schemas [n:s3]
- CI blocked breaking changes before deployment [n:s3]
- Kept producers and consumers compatible as they evolved
- Zero consumer breakages across 140 schema changes in 18 months [n:s3]

*Grade: grounded False, invented [], complete 5, correct 5, spoken 5, shape 5: The producer/consumer compatibility point is supported by the digest but lacks an [n:s3] citation.*

## schema_changes (technical), deep pass

> I'm curious how you handled schema changes in that pipeline

Introduced a schema registry with backward-compatible Avro schemas [n:s3]
Added a CI check that blocks breaking changes [n:s3]
I’d add fields with defaults; old events stay readable
I’d check compatibility against every schema still being replayed
Main pitfall I’d watch: backward compatibility alone misses old consumers
Zero consumer breakages in 18 months after 140 schema changes [n:s3]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## process_thread (technical, not covered), fast pass

> What's the difference between a process and a thread, and when would you pick one over the other?

- Processes have separate memory; threads share memory within a process
- I'd pick processes for isolation, separate lifecycles, or fault containment
- I'd pick threads for shared state and lower-overhead concurrent work
- Shared memory needs synchronization; thread bugs can affect the whole process

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## process_thread (technical, not covered), deep pass

> What's the difference between a process and a thread, and when would you pick one over the other?

Processes isolate address spaces; threads share one process’s memory
Each thread has its own stack, registers, scheduling state
Processes communicate through pipes, sockets, or explicitly shared memory
I’d pick processes for fault isolation or Python CPU-bound work
I’d pick threads for shared-state concurrency or blocking I/O
Tradeoff: process overhead versus thread races and shared crash risk
Python caveat: standard CPython’s GIL limits CPU-bound threads

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## do_differently (behavioral), fast pass

> What would you do differently if you built it again

- I’d identify real-world validation needs before launch
- Use shadow mode before exposing predictions [n:s8]
- Measure accuracy by region before rollout [n:s8]
- Keep rollback ready if results miss targets [n:s8]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 3: Short, usable cues, but they assume the ETA project rather than addressing what to change in the pipeline discussed on the call.*

## do_differently (behavioral), deep pass

> What would you do differently if you built it again

I'd define schema ownership and compatibility rules before migration
I'd test old consumers against new schemas in CI
I'd test replay, duplicate events, and out-of-order delivery earlier
I'd set cutover checks for lag and reconciliation accuracy
I'd keep rollback ready until those checks consistently pass

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong practical cues, but a brief link to the original pipeline migration would make the retrospective more specific.*

## index (technical, not covered), fast pass

> How does a database index actually speed up a query?

- Index keeps ordered keys with row pointers
- B-tree narrows lookup instead of scanning every row
- Costs extra storage and slower writes
- I'd check selectivity and query plan before adding one

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## index (technical, not covered), deep pass

> How does a database index actually speed up a query?

I’d describe an index as shortcuts to matching rows
B-tree: sorted keys, branching pages; lookup roughly O(log N)
Leaf entries reference rows; covering indexes can avoid row fetches
Composite indexes generally work best with leading-column filters
Tradeoff: extra storage, slower writes; broad queries may favor scans
Two indexes and a cache cut p95 70% [n:s4]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## manager (behavioral), fast pass

> Have you ever disagreed with a decision your manager made? What happened?

- I’d show the evidence, then suggest a small test
- Tracking-service rewrite disagreement [n:s4]
- Proposed a two-week spike with measurable criteria [n:s4]
- Two indexes and a cache; p95 down 70% [n:s4]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong story cues, but omit that the spike identified the database bottleneck and the manager agreed to drop the rewrite.*

## manager (behavioral), deep pass

> Have you ever disagreed with a decision your manager made? What happened?

Disagreed with manager on a rewrite [n:s4]
Manager wanted to rewrite tracking service in a new language [n:s4]
I proposed a two-week spike with measurable criteria instead [n:s4]
Spike showed the bottleneck was the database [n:s4]
Fixed with two indexes and a cache; p95 down 70% [n:s4]
Manager agreed and the rewrite was dropped [n:s4]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## url (technical, not covered), fast pass

> Walk me through what happens when you type a URL into the browser and hit enter.

- Browser parses URL, checks cache, resolves DNS
- Opens TCP connection; TLS handshake for HTTPS
- Sends HTTP request; server routes and returns response
- Browser parses HTML, fetches assets, builds and paints page

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## url (technical, not covered), deep pass

> Walk me through what happens when you type a URL into the browser and hit enter.

I'd trace URL parsing through networking, HTTP, and rendering
Browser checks caches; DNS resolves hostname to IP
HTTPS: TCP then TLS; HTTP/3 uses QUIC instead
HTTP request carries path, headers, cookies; response brings status, content
Browser builds DOM/CSSOM, fetches resources, runs JavaScript, lays out, paints
I'd watch render-blocking resources; caching trades freshness for speed

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## leaving (behavioral), fast pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

- Roadmap shifting toward frontend; I want backend
- Larger-scale payments, like your streaming settlement work [n:s2]
- More system design ownership
- A role that keeps me deep in backend work

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 4: The fourth cue repeats the first; three points would be tighter.*

## leaving (behavioral), deep pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

Current team's roadmap is shifting to frontend work [n:a1]
I want to stay deep in backend
Looking for larger-scale payments and more system design ownership
Your batch-to-streaming settlement work fits that direction
Moved reconciliation from batch to Kafka streaming [n:s2]
Lag went from 24 hours to under 2 minutes [n:s2]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## questions_for_us (other), fast pass

> We're nearly out of time. Do you have any questions for us?

- How far along is the batch-to-streaming move, and what’s hardest so far?
- How do squads split ownership of shared payment services?
- What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## questions_for_us (other), deep pass

> We're nearly out of time. Do you have any questions for us?

How far along is the batch-to-streaming settlement move?
What’s been hardest: correctness, migration, or operating the pipeline?
How much system design ownership would this role have?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## idempotency (technical, not covered), fast pass

> How would you make a payment API idempotent, so a retried request never charges the customer twice?

- Require an idempotency key per payment attempt
- I'd persist key, request hash, and outcome atomically
- Same key and payload returns the saved result; reject mismatches
- Coordinate with the processor; timeouts can hide a completed charge

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good opening cues, but missing explicit concurrent-duplicate handling, a retention window, and reuse of an idempotency key at the payment provider.*

## idempotency (technical, not covered), deep pass

> How would you make a payment API idempotent, so a retried request never charges the customer twice?

I’d use durable idempotency records, plus provider-side idempotency
I’d require client keys; PostgreSQL unique constraint per merchant/key
I’d store payload hashes; reject changed payloads, replay stored responses
I’d atomically claim requests; concurrent retries return pending
I’d reuse provider keys; reconcile timeouts before retrying charges
Main pitfall: database transactions alone can’t prevent duplicate external charges

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong, concise design covering concurrency and provider idempotency, but omits a retention window.*

## k8s_probes (technical, not covered), fast pass

> What's the difference between a liveness probe and a readiness probe in Kubernetes?

- Liveness asks whether the process is stuck and needs restarting
- Readiness asks whether it can take traffic right now
- Failed readiness removes it from service; failed liveness restarts it
- I'd keep checks lightweight, avoiding dependency failures that trigger restart loops

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise distinction; misses readiness for warm-up and dependencies and startup probes for slow starts.*

## k8s_probes (technical, not covered), deep pass

> What's the difference between a liveness probe and a readiness probe in Kubernetes?

Liveness: should Kubernetes restart it; readiness: should it receive traffic
I’d use HTTP, TCP, or exec checks; gRPC also supported
Liveness failures restart the container after failureThreshold consecutive failures
Readiness failures remove Pod from Service endpoints; container keeps running
Defaults: periodSeconds 10, timeoutSeconds 1, failureThreshold 3
I’d avoid dependency-based liveness checks; risk of cascading restarts
I’d use startup probes for slow starts; gate both checks

*Grade: grounded True, invented [], complete 4, correct 5, spoken 4, shape 5: Accurate and well-shaped, but readiness for warm-up and dependency health is missing; defaults are less useful than that distinction.*

## isolation (technical, not covered), fast pass

> Can you walk me through transaction isolation levels and what each one protects you from?

- I'd compare dirty reads, non-repeatable reads, and phantoms
- Read uncommitted permits dirty reads; read committed prevents them
- Repeatable read keeps reread rows stable; phantom behavior varies by database
- Serializable prevents conflicting concurrent effects; costs concurrency and can abort transactions

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Clear short cues, but omit write skew or lost updates, PostgreSQL's default and snapshot isolation behavior, and retrying serialization failures.*

## isolation (technical, not covered), deep pass

> Can you walk me through transaction isolation levels and what each one protects you from?

I’d frame isolation as visibility guarantees between concurrent transactions
Read Uncommitted: dirty reads possible; weakest standard isolation
Read Committed: prevents dirty reads; nonrepeatable reads and phantoms remain
Repeatable Read: also prevents nonrepeatable reads; standard permits phantoms
Serializable: prevents serialization anomalies; equivalent to some serial execution
PostgreSQL: Read Committed default; Repeatable Read blocks phantoms, permits write skew
I’d expect blocking or retries; PostgreSQL treats Uncommitted as Committed

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## exactly_once (technical, not covered), fast pass

> Kafka says it supports exactly-once. What does that actually mean, and where does it break down?

- Kafka exactly-once: transactional writes and offset commits
- Keep output and consumed offsets in one Kafka transaction
- External side effects need idempotency or their own transaction
- Retries, replays, and misconfigured producers can still duplicate effects

*Grade: grounded True, invented [], complete 3, correct 4, spoken 5, shape 5: Good short cues, but omit read_committed, idempotent producers, fencing and costs; an external transaction alone does not coordinate side effects with Kafka offsets.*

## exactly_once (technical, not covered), deep pass

> Kafka says it supports exactly-once. What does that actually mean, and where does it break down?

I’d define exactly-once as once-only effects within Kafka’s transaction boundary
Idempotent producers deduplicate retries using producer IDs and sequence numbers
Transactions atomically commit output records and consumed offsets together
Consumers need read_committed; default read_uncommitted can expose aborted records
Processing can repeat; external database writes aren’t automatically included
I’d use idempotency keys for payments and external writes
Tradeoff: transaction overhead, commit latency, and more operational complexity

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong answer; omits zombie-producer fencing via transactional IDs and explicit atomicity across partitions.*

## ledger (system_design, not covered), fast pass

> How would you design a ledger service for a payments platform that never loses or double-counts money?

- I'd use immutable double-entry journal entries
- PostgreSQL transactions; balance each entry, unique idempotency keys
- Outbox plus Kafka; consumers dedupe, reconcile streams to ledger
- Never trust “exactly once”; audit corrections, backups, restore tests

*Grade: grounded True, invented [], complete 3, correct 4, spoken 5, shape 5: Good concise opening, but misses requirements clarification, account-level consistency, balance derivation and provider reconciliation; balance transactions, not individual debit or credit entries.*

## ledger (system_design, not covered), deep pass

> How would you design a ledger service for a payments platform that never loses or double-counts money?

Are we supporting multiple currencies?
What throughput and posting latency targets?
I'd use PostgreSQL, immutable double-entry postings, integer minor units
I'd balance debits and credits separately per currency
I'd atomically write postings, idempotency keys, and outbox events
I'd reject reused keys with different request payloads
I'd serialize balance checks; prevent unauthorized overdrafts
I'd acknowledge only durable commits; retry unknown outcomes idempotently
I'd publish outbox events to Kafka; deduplicate consumers
I'd reconcile provider statements; correct errors through reversing entries
I'd scale reads with projections; preserve atomicity across accounts
I'd trade availability for correctness during partitions
I'd require tested backups, failover fencing, and synchronous replication

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## notifications (system_design, not covered), fast pass

> Design a system that sends email and push notifications to fifty million users.

- I’d split ingestion, fan-out, delivery, and preference services
- Partition by user ID; queue work through Kafka
- Rate-limit providers, retry transient errors with backoff, dead-letter poison messages
- Track delivery status and deduplicate; avoid promising exactly-once delivery

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Good concise architecture cues, but misses clarifying traffic and latency, templates and opt-outs, and explicit horizontal worker scaling.*

## notifications (system_design, not covered), deep pass

> Design a system that sends email and push notifications to fifty million users.

Transactional alerts or campaigns; what delivery latency matters?
Fifty million daily recipients, or simultaneous sends; which regions?
I'd separate ingestion, scheduling, Kafka queues, and channel workers
I'd store notification IDs, preferences, device tokens, delivery status
I'd publish committed notification requests through PostgreSQL transactional outbox
I'd fan out audiences asynchronously, partitioning work by user
I'd recheck consent before sending through email and push providers
I'd use at-least-once delivery, idempotency keys; provider deduplication varies
I'd retry transient failures with jitter; dead-letter permanent failures
I'd expect ambiguous provider timeouts; duplicate delivery remains possible
I'd autoscale on lag, respecting provider quotas and backpressure
I'd isolate urgent alerts; trade campaign latency for cost

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Strong design cues, but omits templates and explicit exponential backoff and exceeds the 110-word limit.*

## learn_fast (behavioral, not covered), fast pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- I’d learn the smallest slice needed, then test it against the deadline
- For a real example, moving reconciliation to Kafka streaming [n:s2]
- Ran old and new side by side for six weeks before cutover [n:s2]
- Lag went from 24 hours to under 2 minutes [n:s2]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 4: Kafka is only a loose match; acknowledge no documented deadline-learning story and add concrete learning steps such as docs, a spike, and asking experts.*

## learn_fast (behavioral, not covered), deep pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

I'd start with the deadline and smallest working outcome
I'd identify the unfamiliar parts most likely to block delivery
I'd use official docs and build a small spike
I'd test failure cases before committing to the approach
I'd timebox learning, leaving room for testing and rollout
I'd flag risks early and cut scope before cutting safeguards

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good honest approach, but misses asking experts and shipping behind a flag.*

## bad_news (behavioral, not covered), fast pass

> Tell me about a time you had to deliver bad news to a customer or a stakeholder.

- I’d be direct about the impact and what’s known
- Share the next steps and when I’ll update them
- Closest story: rural ETA predictions were off by hours; we rolled back [n:s8]
- Added shadow mode and per-region accuracy dashboards before relaunch [n:s8]

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Honest closest-story framing and clear update plan; could explicitly say to tell stakeholders early.*

## bad_news (behavioral, not covered), deep pass

> Tell me about a time you had to deliver bad news to a customer or a stakeholder.

I'd share bad news early, before the next decision
I'd state what's wrong, who's affected, and what's uncertain
I'd own my part, without blaming another team
I'd explain recovery options, tradeoffs, and my recommendation
I'd give the next update time, not an unverified fix date
I'd follow through with progress, even without a resolution

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## time_zones (behavioral, not covered), fast pass

> How have you handled working with a team spread across several time zones?

- I’d make ownership and handoffs explicit across time zones
- Keep decisions and context in written updates
- Use overlap time for complex discussions, not routine status
- Set clear escalation paths for time-sensitive issues

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong approach cues, but should briefly acknowledge the lack of a specific distributed-team story.*

## time_zones (behavioral, not covered), deep pass

> How have you handled working with a team spread across several time zones?

I'd default to async updates, keeping progress visible across time zones
I'd write decisions down, including context, owners, and next steps
I'd reserve overlap hours for blockers and tricky discussions
I'd rotate meeting times, sharing the early and late calls
I'd leave clear handoffs, so work continues without waiting
I'd agree escalation paths, so urgent issues reach someone awake

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## why_company (other, not covered), fast pass

> Why do you want to work at Northwind Payments specifically?

- Northwind’s batch-to-streaming payments work fits my experience [n:s2]
- I want larger-scale payments and more system design ownership
- I’d like to help move settlement toward streaming
- How far along is that move, and what’s hardest so far?

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Good core fit and goals, but the third cue repeats the first and the closing question could be replaced with a more specific reason for Northwind.*

## why_company (other, not covered), deep pass

> Why do you want to work at Northwind Payments specifically?

Your batch-to-streaming move fits my order pipeline work [n:s2]
Reconciliation lag: 24 hours to under 2 minutes [n:s2]
I want larger-scale payments and more system design ownership
I'd like tackling settlement correctness, retries, and replay safety

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## salary (other, not covered), fast pass

> What are your salary expectations for this role?

- I’m looking for a larger-scale payments role with more system design ownership
- Could you share the budgeted range for the role?
- I’d weigh the full package and scope together
- I’m open to a conversation once I understand the range

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## salary (other, not covered), deep pass

> What are your salary expectations for this role?

I’d weigh the whole package, including equity and benefits
More system design ownership matters to me
Your streaming settlement work fits that direction
I’d be open for the right scope
What’s the salary range budgeted for this role?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## culture (other, not covered), fast pass

> What kind of team culture do you do your best work in?

- Clear ownership, shared service boundaries, open disagreement
- I do best with measurable experiments over assumptions
- Streaming work and system design ownership fit my goals
- I'd ask how squads coordinate across shared payment services

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 3: Clear ownership and measurable experiments fit, but the last two cues drift from team culture and omit supporting stories such as the measured spike or runbook.*

## culture (other, not covered), deep pass

> What kind of team culture do you do your best work in?

I’d look for clear ownership and room for technical debate
I’d want disagreements settled through small, measurable experiments
Proposed a two-week spike instead of a rewrite [n:s4]
Database fixes brought p95 down 70%; rewrite dropped [n:s4]
For your streaming move, I’d value shared decisions across squads

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear working-style cues backed by the measured-spike story; blameless reviews or written design would make the culture answer fuller.*

## five_years (other, not covered), fast pass

> Where do you see yourself in five years?

- Deeper ownership of large-scale payments systems
- Leading design across streaming settlement services
- Staying hands-on with Go, Python, Kafka
- Growing into broader technical leadership, not away from engineering

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Clear future-facing cues, but broader technical leadership should explicitly include mentoring others and owning reliability.*

## five_years (other, not covered), deep pass

> Where do you see yourself in five years?

In five years, I’d want broader ownership across payments systems
Still hands-on, with more responsibility for system design
Here, I’d aim to help shape streaming settlement
Order pipeline to Kafka: lag under 2 minutes [n:s2]
I’d build on that, taking on larger-scale payments problems

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Clear, grounded future goals tied to streaming payments, but misses mentoring others and ownership of reliability.*

## first_unique (coding, not covered), fast pass

> How would you find the first non-repeating character in a string?

- Count each character in one pass
- Scan again; return the first count of one
- O(n) time, O(k) space for distinct characters
- Define Unicode handling and case sensitivity first

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Good fast-pass cues, but omit what to return for an empty string or when no character is unique.*

## first_unique (coding, not covered), deep pass

> How would you find the first non-repeating character in a string?

Approach: I'd count characters, then scan left-to-right for count one
Complexity: O(n) time, O(k) space for distinct characters
Edge cases: Empty or all repeated → none; clarify Unicode character boundaries

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Strong concise answer; misses clarifying case sensitivity.*
