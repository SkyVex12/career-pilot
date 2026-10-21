All questions:

| | quality-before fast | quality-before deep | quality-after fast | quality-after deep |
|---|---|---|---|---|
| Answers graded | 52 | 52 | 52 | 52 |
| Grounded (cited node supports it) | 0.88 | 0.98 | 0.96 | 1.0 |
| No invented facts | 0.92 | 0.98 | 0.98 | 1.0 |
| Complete, 1 to 5 (grader) | 4.04 | 4.31 | 4.06 | 4.67 |
| Technically correct, 1 to 5 (grader) | 4.88 | 5 | 4.94 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.9 | 4.94 | 5 | 4.94 |
| Length and shape, 1 to 5 (grader) | 4.77 | 4.87 | 4.79 | 4.98 |
| Words per answer | 32.2 | 42.5 | 35.4 | 55.7 |
| Words per line | 8.5 | 8.6 | 8.9 | 8.8 |
| Lines with a citation | 0.35 | 0.42 | 0.23 | 0.25 |
| Citations of unknown ids (count) | 0 | 0 | 0 | 0 |
| Stock phrases per answer | 0.02 | 0 | 0 | 0.02 |
| Full written sentences per answer | 0 | 1.37 | 0 | 0 |

Questions the profile does not cover:

| | quality-before fast | quality-before deep | quality-after fast | quality-after deep |
|---|---|---|---|---|
| Answers graded | 34 | 34 | 34 | 34 |
| Grounded (cited node supports it) | 0.85 | 0.97 | 1.0 | 1.0 |
| No invented facts | 0.88 | 0.97 | 1.0 | 1.0 |
| Complete, 1 to 5 (grader) | 3.85 | 4.09 | 3.88 | 4.53 |
| Technically correct, 1 to 5 (grader) | 4.82 | 5 | 4.91 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.85 | 4.91 | 5 | 4.91 |
| Length and shape, 1 to 5 (grader) | 4.85 | 4.82 | 4.85 | 4.97 |
| Words per answer | 33.7 | 42.1 | 35.8 | 58.3 |
| Words per line | 8.6 | 8.8 | 9.0 | 9.0 |
| Lines with a citation | 0.17 | 0.29 | 0.07 | 0.07 |
| Citations of unknown ids (count) | 0 | 0 | 0 | 0 |
| Stock phrases per answer | 0.03 | 0 | 0 | 0.03 |
| Full written sentences per answer | 0 | 1.85 | 0 | 0 |

**incident** (behavioral): Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - Black Friday: payments adapter exhausted connections at 6x normal traffic [n:s1]<br>- Led the incident; shed load with a feature flag in 12 minutes [n:s1]<br>- Revenue loss held under 0.3% of the day [n:s1]<br>- Added per-provider pool limits and load tests in CI [n:s1] | - Black Friday payments outage: pool exhausted at 6x traffic [n:s1]<br>- Led the incident; shed load with a feature flag in 12 minutes [n:s1]<br>- Revenue loss held under 0.3% of the day [n:s1]<br>- Added per-provider pool limits and load tests in CI [n:s1] |
| complete 5, correct 5, spoken 5, shape 5, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Checkout outage on Black Friday [n:s1]<br>Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]<br>Led the incident; shed load with a feature flag [n:s1]<br>Load shed in 12 minutes; revenue loss under 0.3% of the day [n:s1]<br>Then added pool limits per provider and load tests in CI [n:s1] | Checkout outage on Black Friday [n:s1]<br>Payments adapter exhausted its connection pool at 6x normal traffic [n:s1]<br>Led the incident; shed load with a feature flag in 12 minutes [n:s1]<br>Revenue loss held under 0.3% of the day [n:s1]<br>Then added pool limits per provider and load tests in CI [n:s1] |
| complete 5, correct 5, spoken 5, shape 5, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

**rate_limiter** (system_design): How would you design a rate limiter for a public API that has to work across several regions?

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - Token bucket per API key, Redis with atomic Lua updates<br>- Key by API key and region; shared global quota needs coordinated state<br>- Local in-process buckets during Redis failover [n:s6]<br>- Set limits, refill rate, and fail-open or fail-closed by endpoint | - I’d use a distributed token bucket per API key, with explicit burst and refill limits<br>- Keep regional counters in Redis or a fast shared store; partition keys to avoid hot spots<br>- For global limits, coordinate quota across regions; trade strict accuracy for latency and availability<br>- Fail open or use local buckets during store outages; cap fallback traffic and monitor drift [n:s6] |
| complete 3, correct 5, spoken 5, shape 5, invented 0 | complete 4, correct 5, spoken 5, shape 5, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Is the limit global per API key, or per region?<br>How much overshoot is acceptable, and what latency budget?<br>Components: gateway, regional Redis token buckets, global quota allocator.<br>Data flow: allocate regional quotas; atomically consume tokens; reject with Retry-After.<br>Scaling: shard by API key; rebalance regional quotas with demand.<br>Tradeoffs: regional quotas reduce latency; strict global limits need coordination. | Are limits per API key, IP, or tenant?<br>How strict globally, and what latency budget?<br>I'd use gateway checks with per-key token buckets<br>I'd allocate regional quotas from a global coordinator<br>I'd keep summed allocations within the global limit<br>I'd check regional Redis buckets atomically using Lua<br>I'd return 429 with Retry-After when tokens run out<br>I'd shard by key; batch quota leases regionally<br>I'd expire leases before reallocating, preventing double spending<br>I'd bound local fallback; fail closed for sensitive endpoints<br>I'd trade cross-region coordination for temporarily stranded quota<br>Previously: Redis Lua, local fallback, 15k requests per second [n:s6] |
| complete 4, correct 5, spoken 5, shape 5, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

**idempotency** (technical, not covered by the profile): How would you make a payment API idempotent, so a retried request never charges the customer twice?

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - Idempotency key per payment request; scope it to the customer<br>- Store key, request hash, and payment result durably<br>- Atomically claim the key before charging; duplicates return the stored result<br>- Keep the claim through retries and timeouts; reconcile uncertain outcomes before retrying the charge | - Require an idempotency key per payment attempt<br>- I'd persist key, request hash, and outcome atomically<br>- Same key and payload returns the saved result; reject mismatches<br>- Coordinate with the processor; timeouts can hide a completed charge |
| complete 3, correct 5, spoken 5, shape 5, invented 0 | complete 3, correct 5, spoken 5, shape 5, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Require an idempotency key for each logical payment.<br>Atomically claim merchant-plus-key; store request hash and payment status.<br>Reject changed payloads; retries return saved results or pending.<br>Reuse that key with provider; reconcile timeouts before retrying.<br>My payments adapter layer: same boundary to protect. [n:a1]<br>Tradeoff: durable state and reconciliation; guarantees depend on provider support. | I’d use durable idempotency records, plus provider-side idempotency<br>I’d require client keys; PostgreSQL unique constraint per merchant/key<br>I’d store payload hashes; reject changed payloads, replay stored responses<br>I’d atomically claim requests; concurrent retries return pending<br>I’d reuse provider keys; reconcile timeouts before retrying charges<br>Main pitfall: database transactions alone can’t prevent duplicate external charges |
| complete 4, correct 5, spoken 5, shape 5, invented 0 | complete 4, correct 5, spoken 5, shape 5, invented 0 |

**learn_fast** (behavioral, not covered by the profile): Tell me about a time you had to learn a new technology quickly to hit a deadline.

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - Order pipeline to Kafka: learned Go consumers quickly [n:s2]<br>- Replaced nightly batch reconciliation with streaming<br>- Ran old and new side by side for six weeks<br>- Reconciliation lag: 24 hours to under 2 minutes [n:s2] | - I’d learn the smallest slice needed, then test it against the deadline<br>- For a real example, moving reconciliation to Kafka streaming [n:s2]<br>- Ran old and new side by side for six weeks before cutover [n:s2]<br>- Lag went from 24 hours to under 2 minutes [n:s2] |
| complete 2, correct 5, spoken 5, shape 4, invented 1 | complete 2, correct 5, spoken 5, shape 4, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Order pipeline to Kafka [n:s2]<br>Nightly batch reconciliation; lag of 24 hours [n:s2]<br>Replaced batch with Kafka, Go consumers, PostgreSQL [n:s2]<br>Ran old and new side by side for 6 weeks [n:s2]<br>After cutover: reconciliation lag under 2 minutes [n:s2] | I'd start with the deadline and smallest working outcome<br>I'd identify the unfamiliar parts most likely to block delivery<br>I'd use official docs and build a small spike<br>I'd test failure cases before committing to the approach<br>I'd timebox learning, leaving room for testing and rollout<br>I'd flag risks early and cut scope before cutting safeguards |
| complete 2, correct 5, spoken 5, shape 3, invented 0 | complete 4, correct 5, spoken 5, shape 5, invented 0 |

**salary** (other, not covered by the profile): What are your salary expectations for this role?

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - No salary range in my notes<br>- Could you share the range budgeted for the role?<br>- I’d compare it with the scope and total compensation | - I’m looking for a larger-scale payments role with more system design ownership<br>- Could you share the budgeted range for the role?<br>- I’d weigh the full package and scope together<br>- I’m open to a conversation once I understand the range |
| complete 4, correct 5, spoken 3, shape 4, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Looking at the full package, not just base salary<br>What range have you budgeted for this role?<br>Happy to discuss within that range, depending on scope | I’d weigh the whole package, including equity and benefits<br>More system design ownership matters to me<br>Your streaming settlement work fits that direction<br>I’d be open for the right scope<br>What’s the salary range budgeted for this role? |
| complete 5, correct 5, spoken 5, shape 5, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

**why_company** (other, not covered by the profile): Why do you want to work at Northwind Payments specifically?

| fast pass, quality-before | fast pass, quality-after |
|---|---|
| - Batch-to-streaming payments work, like my Kafka reconciliation project [n:s2]<br>- Bigger payments scale and more backend system design ownership<br>- Go, PostgreSQL, and Kafka match my platform experience [n:a1] | - Northwind’s batch-to-streaming payments work fits my experience [n:s2]<br>- I want larger-scale payments and more system design ownership<br>- I’d like to help move settlement toward streaming<br>- How far along is that move, and what’s hardest so far? |
| complete 5, correct 5, spoken 5, shape 5, invented 0 | complete 4, correct 5, spoken 5, shape 4, invented 0 |

| deep pass, quality-before | deep pass, quality-after |
|---|---|
| Your batch-to-streaming move matches my order pipeline to Kafka work [n:s2]<br>Reconciliation lag: 24 hours to under 2 minutes [n:s2]<br>Looking for larger-scale payments and more system design ownership<br>Current team's roadmap shifting to frontend work | Your batch-to-streaming move fits my order pipeline work [n:s2]<br>Reconciliation lag: 24 hours to under 2 minutes [n:s2]<br>I want larger-scale payments and more system design ownership<br>I'd like tackling settlement correctness, retries, and replay safety |
| complete 4, correct 5, spoken 5, shape 5, invented 0 | complete 5, correct 5, spoken 5, shape 5, invented 0 |

