# Answer samples: after

| | after fast | after deep |
|---|---|---|
| Answers graded | 24 | 24 |
| Grounded (cited node supports it) | 0.96 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Sounds spoken, 1 to 5 (grader) | 5 | 5 |
| Length and shape, 1 to 5 (grader) | 4.75 | 4.92 |
| Words per answer | 31.4 | 45.6 |
| Words per line | 8.5 | 8.5 |
| Lines with a citation | 0.52 | 0.52 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 1.08 |

## background (behavioral), fast pass

> So to kick things off, can you walk me through your background and what you're working on right now?

- Eight years backend; senior on Larkspur’s checkout platform [n:a1]
- Own the order pipeline; moved reconciliation to Kafka streaming [n:s2]
- Before that, tracking APIs and event ingestion at Tidewater [n:a2]
- Looking for larger-scale payments and more system design ownership

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## background (behavioral), deep pass

> So to kick things off, can you walk me through your background and what you're working on right now?

Current role: Senior Backend Engineer, Larkspur Commerce [n:a1]
Before that: tracking APIs and event ingestion at Tidewater [n:a2]
Now own the order pipeline and payments adapter layer [n:a1]
Order pipeline to Kafka: replaced nightly batch reconciliation [n:s2]
Built streaming pipeline: Kafka, Go consumers, PostgreSQL [n:s2]
Reconciliation lag: 24 hours to under 2 minutes [n:s2]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## incident (behavioral), fast pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

- Black Friday: payments adapter out of connections at 6x traffic [n:s1]
- Led the incident; shed load with a feature flag in 12 minutes [n:s1]
- Revenue loss held under 0.3% of the day's revenue [n:s1]
- Added per-provider pool limits and load tests in CI [n:s1]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## incident (behavioral), deep pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

Checkout outage on Black Friday [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Led the incident; shed load with a feature flag in 12 minutes [n:s1]
Revenue loss held under 0.3% of the day [n:s1]
Then added pool limits per provider and load tests in CI [n:s1]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## incident_followup (behavioral), fast pass

> And what did you change afterwards so it wouldn't happen again?

- Black Friday: per-provider pool limits [n:s1]
- Load tests added in CI [n:s1]
- Feature flag shed load in 12 minutes [n:s1]

*Grade: grounded True, invented [], spoken 5, shape 4: The feature-flag point describes incident mitigation, not a change made afterwards to prevent recurrence.*

## incident_followup (behavioral), deep pass

> And what did you change afterwards so it wouldn't happen again?

Black Friday checkout outage [n:s1]
Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]
Added pool limits per provider to contain connection exhaustion [n:s1]
Added load tests in CI for traffic spikes [n:s1]
Revenue loss held under 0.3% of the day [n:s1]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## rate_limiter (system_design), fast pass

> How would you design a rate limiter for a public API that has to work across several regions?

- Token bucket per API key, atomic Redis updates with Lua
- Regional Redis, local in-process buckets during failover [n:s6]
- Set global limits with coordinated quota allocation across regions
- Handle clock skew, retries, and Redis outages; return 429 with retry guidance

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## rate_limiter (system_design), deep pass

> How would you design a rate limiter for a public API that has to work across several regions?

- Clarify: strict global limit, or some regional overshoot acceptable?
- Clarify: limits per API key, endpoint, or both?
- Components: regional gateways, Redis token buckets, global quota allocator.
- Data flow: authenticate key; atomic Lua check; allow or 429.
- Scaling: shard by key; lease regional quotas within global budget.
- Tradeoffs: leases reduce latency; strict global checks add coordination.
- Failures: bounded local fallback; accept overshoot or reject requests.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## schema_changes (technical), fast pass

> I'm curious how you handled schema changes in that pipeline

- Schema evolution: backward-compatible Avro schemas [n:s3]
- Schema registry with a CI check blocking breaking changes [n:s3]
- Zero consumer breakages across 140 schema changes in 18 months [n:s3]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## schema_changes (technical), deep pass

> I'm curious how you handled schema changes in that pipeline

Schema registry with backward-compatible Avro schemas [n:s3]
CI check blocks breaking changes before they ship [n:s3]
Add optional fields with defaults; keep existing readers working
Zero consumer breakages in 18 months after 140 schema changes [n:s3]
Tradeoff: breaking changes need staged migrations, not immediate replacement

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## process_thread (technical), fast pass

> What's the difference between a process and a thread, and when would you pick one over the other?

- Process: separate memory space; stronger isolation
- Thread: shared memory within one process; cheaper communication
- Pick processes for isolation or independent failure boundaries
- Pick threads for shared-state work with low communication overhead

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## process_thread (technical), deep pass

> What's the difference between a process and a thread, and when would you pick one over the other?

Process: separate memory; thread: shared memory within one process.
OS schedules both; threads share heap, keep separate stacks.
Processes communicate through messages; threads need synchronization around shared data.
For Kafka Go consumers, goroutines—not OS threads directly. [n:s2]
Pick processes for isolation; threads for shared-memory concurrency.
Tradeoff: process overhead versus thread races and shared crashes.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## do_differently (behavioral), fast pass

> What would you do differently if you built it again

- Order pipeline to Kafka: run old and new side by side for six weeks [n:s2]
- Keep that migration plan; reconciliation lag under two minutes [n:s2]
- Add schema registry and CI compatibility checks early [n:s3]
- Zero consumer breakages after 140 schema changes [n:s3]

*Grade: grounded True, invented [], spoken 5, shape 3: Short, grounded cues, but mostly recap results and what to keep rather than explaining what to do differently and why.*

## do_differently (behavioral), deep pass

> What would you do differently if you built it again

Order pipeline to Kafka — schema evolution [n:s2] [n:s3]
Replaced nightly batch reconciliation with a streaming pipeline [n:s2]
Introduced schema registry, backward-compatible Avro schemas, CI compatibility check [n:s3]
Zero consumer breakages in 18 months after 140 schema changes [n:s3]
Building again: define compatibility rules before moving producers
Add consumer replay tests before the first cutover

*Grade: grounded True, invented [], spoken 5, shape 4: Clear cues, but blends two stories and gives little explanation of why the proposed changes would help.*

## index (technical), fast pass

> How does a database index actually speed up a query?

- B-tree index, sorted keys and row pointers
- Find matches without scanning every row
- Fewer page reads; tradeoff is write and storage cost
- Composite indexes help when filters match the leading columns

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## index (technical), deep pass

> How does a database index actually speed up a query?

An index finds matching rows without scanning the whole table.
B-tree indexes keep keys sorted, pointing to matching rows.
The database follows tree branches, then fetches those rows.
Tracking service: two indexes and a cache; p95 down 70%. [n:s4]
Tradeoff: extra storage, slower writes from maintaining the index.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## manager (behavioral), fast pass

> Have you ever disagreed with a decision your manager made? What happened?

- Manager wanted a tracking-service rewrite in a new language [n:s4]
- Proposed a two-week spike with measurable criteria [n:s4]
- Database was the bottleneck; two indexes and a cache [n:s4]
- p95 down 70%, and we dropped the rewrite [n:s4]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## manager (behavioral), deep pass

> Have you ever disagreed with a decision your manager made? What happened?

Disagreed with manager on a rewrite [n:s4]
Manager wanted tracking service rewritten in a new language [n:s4]
Proposed a two-week spike with measurable criteria instead [n:s4]
Database bottleneck; fixed with two indexes and a cache [n:s4]
p95 down 70%; manager agreed, rewrite dropped [n:s4]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## url (technical), fast pass

> Walk me through what happens when you type a URL into the browser and hit enter.

- Browser parses URL; checks cache and service worker
- DNS resolves hostname to an IP
- TCP connection, then TLS handshake for HTTPS
- Sends HTTP request; server responds, browser parses HTML and loads resources

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## url (technical), deep pass

> Walk me through what happens when you type a URL into the browser and hit enter.

Browser finds the server, requests content, then renders the page.
DNS resolves hostname; browser connects using TCP/TLS or QUIC.
HTTP response arrives; browser parses HTML, fetches resources, runs JavaScript.
Layout and paint turn those resources into visible pixels.
For checkout API requests: payments adapter connection pool matters. [n:s1]
Caching cuts latency, but risks serving stale content.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## leaving (behavioral), fast pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

- Roadmap shifting to frontend; want to stay deep in backend
- Larger-scale payments, like streaming settlement work [n:s2]
- More system design ownership

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## leaving (behavioral), deep pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

Current role: Larkspur Commerce, Checkout Platform [n:a1]
Our roadmap is shifting to frontend work
I want to stay deep in backend
Moved reconciliation from batch to Kafka streaming [n:s2]
Lag: 24 hours to under 2 minutes [n:s2]
Looking for larger-scale payments and more system design ownership

*Grade: grounded True, invented [], spoken 5, shape 4: Clear, grounded cues, but the opening names the role rather than the story and the migration result slightly distracts from the reason for leaving.*

## questions_for_us (other), fast pass

> We're nearly out of time. Do you have any questions for us?

- How far along is the batch-to-streaming move, and what’s hardest so far?
- How do squads split ownership of shared payment services?
- What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## questions_for_us (other), deep pass

> We're nearly out of time. Do you have any questions for us?

How far along is the batch-to-streaming move?
What is hardest so far?
How do squads split ownership of shared payment services?
What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], spoken 5, shape 5: ok*
