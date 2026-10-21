# Answer samples: before

| | before fast | before deep |
|---|---|---|
| Answers graded | 24 | 24 |
| Grounded (cited node supports it) | 1.0 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Sounds spoken, 1 to 5 (grader) | 4.46 | 2.29 |
| Length and shape, 1 to 5 (grader) | 4.71 | 2.12 |
| Words per answer | 47.9 | 105.5 |
| Words per line | 12.9 | 31.5 |
| Lines with a citation | 0.66 | 0.88 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 3.17 | 3.42 |

## background (behavioral), fast pass

> So to kick things off, can you walk me through your background and what you're working on right now?

- I’ve spent eight years in backend engineering, and I’m now a senior engineer on Larkspur’s checkout platform. [n:a1]
- I own the order pipeline and payments adapter layer. [n:a1]
- I moved reconciliation from nightly batches to Kafka streaming, cutting lag from 24 hours to under two minutes. [n:s2]
- Before that, I built shipment tracking APIs and event ingestion at Tidewater Logistics. [n:a2]

*Grade: grounded True, invented [], spoken 4, shape 5: Good four-point fit, though full sentences feel slightly more scripted than glanceable cues.*

## background (behavioral), deep pass

> So to kick things off, can you walk me through your background and what you're working on right now?

I started in backend engineering at Tidewater Logistics, building shipment tracking APIs and an event ingestion service. Now I’m a Senior Backend Engineer on Larkspur Commerce’s nine-engineer Checkout Platform team. I own the order pipeline and payments adapter layer. [n:a2] [n:a1]

One major project was replacing nightly batch reconciliation with streaming. My task was to make reconciliation much faster while managing the migration safely. I built the pipeline with Kafka, Go consumers, and PostgreSQL, and ran the old and new systems side by side for six weeks before cutover. Reconciliation lag dropped from 24 hours to under two minutes. That’s a good example of the backend platform work I focus on today. [n:s2]

*Grade: grounded True, invented [], spoken 2, shape 2: Supported facts, but roughly 115 words in two prose paragraphs exceed the requested 4–6 cue lines under 70 words.*

## incident (behavioral), fast pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

- On Black Friday, our payments adapter exhausted its connection pool at six times normal traffic. [n:s1]
- I led the incident and shed load with a feature flag in 12 minutes. [n:s1]
- We kept revenue loss under 0.3% of the day. [n:s1]
- Afterwards, I added provider-specific pool limits and load tests in CI. [n:s1]

*Grade: grounded True, invented [], spoken 4, shape 5: Accurate and well sized, though full sentences read more like a script than glanceable cues.*

## incident (behavioral), deep pass

> Tell me about a time you had to deal with a production incident while you were under a lot of pressure.

**Situation:** On Black Friday, our payments adapter exhausted its connection pool at six times normal traffic. Checkout was affected during a critical sales day. [n:s1]

**Task:** I led the incident. My immediate priority was to stabilize checkout and limit the revenue impact. [n:s1]

**Action:** I used a feature flag to shed load within 12 minutes. Afterward, I added connection pool limits per payment provider and load tests in CI to catch the same failure mode before production. [n:s1]

**Result:** We held the revenue loss to under 0.3% of the day’s revenue. My approach was to focus first on mitigation, then on preventing a repeat. [n:s1]

*Grade: grounded True, invented [], spoken 2, shape 2: Grounded, but the roughly 110-word STAR prose exceeds the 70-word limit and reads like a script rather than glanceable cues.*

## incident_followup (behavioral), fast pass

> And what did you change afterwards so it wouldn't happen again?

- I added connection-pool limits per payment provider to prevent pool exhaustion. [n:s1]
- I added load tests to CI so we’d catch traffic-related issues earlier. [n:s1]
- I also used a feature flag to shed load during the incident. [n:s1]

*Grade: grounded True, invented [], spoken 4, shape 4: The feature-flag point describes the incident response, not a change made afterwards to prevent recurrence.*

## incident_followup (behavioral), deep pass

> And what did you change afterwards so it wouldn't happen again?

During that Black Friday incident, our payments adapter exhausted its connection pool at six times normal traffic. I led the response, and we shed load with a feature flag in 12 minutes. [n:s1]

After stabilizing it, my priority was to address the pool exhaustion, not just the immediate symptoms. I added connection pool limits per provider and load tests in CI. Those changes targeted the failure mode and made load testing part of our development process. [n:s1]

We held the incident’s revenue loss below 0.3% of the day. I wouldn’t claim it could never happen again, but we had concrete safeguards against the same problem. [n:s1]

*Grade: grounded True, invented [], spoken 2, shape 2: Grounded, but exceeds 70 words, uses prose paragraphs instead of 4–6 cue lines, and spends too much time recapping rather than focusing on prevention.*

## rate_limiter (system_design), fast pass

> How would you design a rate limiter for a public API that has to work across several regions?

- I’d use a token bucket per API key, with Redis for shared limits across service instances. [n:s6]
- I’d partition keys by region and define whether quotas are regional or globally shared.
- For Redis failures, I’ve used local in-process buckets as a fallback. [n:s6]
- I’d test failover and cross-region behavior, especially how limits stay consistent.

*Grade: grounded True, invented [], spoken 5, shape 4: Good short cues, but leaves the main multi-region tradeoff unresolved: enforcing a global quota versus allocating regional quotas.*

## rate_limiter (system_design), deep pass

> How would you design a rate limiter for a public API that has to work across several regions?

**Clarifying questions**
- Do I need to enforce one strict global limit per API key, or can each region have a budget with some temporary overshoot? [n:s6]
- If the rate-limit store is unavailable, should I prioritize API availability or strict enforcement? [n:s6]

**Components**
- I’d start with a token bucket per API key. At Tidewater, I used Redis with a Lua script and local in-process buckets as a fallback. That handled 15,000 requests per second. [n:s6]
- For several regions, I’d consider regional Redis stores with a share of each key’s global budget. I’d treat that as a design choice to validate, not something I’ve already run across regions. [n:s6]

**Data flow**
- I’d check the key’s bucket before forwarding the request. I’d use a Lua script to make the refill and token deduction atomic. If there’s a token, I’d admit the request. Otherwise, I’d reject it with a retry delay. [n:s6]
- If Redis is unavailable, I’d use a bounded local bucket if availability is the priority. That follows the fallback approach I used at Tidewater. [n:s6]

**Scaling**
- I’d partition buckets by API key. Regional budgets would keep the normal request path local, without a cross-region call for every request. I’d validate the budget split against the expected traffic pattern. [n:s6]
- I’d load-test bursts and store failures before launch. Adding load tests after a production connection-pool incident reinforced that approach for me. [n:s1]

**Tradeoffs**
- I’d choose regional budgets when low latency matters more than perfect global utilization. One region could reject requests while another still has unused capacity. Local fallback buckets can also allow overshoot. [n:s6]
- If the global cap must be strict, I’d favor coordinated decisions and accept the extra latency and availability cost. I’d make that requirement explicit before choosing the architecture. [n:s6]

*Grade: grounded True, invented [], spoken 2, shape 2: Technically relevant and grounded, but far over 100 words, with multiple full-sentence bullets per section instead of one short cue line each.*

## schema_changes (technical), fast pass

> I'm curious how you handled schema changes in that pipeline

- I introduced a schema registry using backward-compatible Avro schemas [n:s3].
- I added a CI check to block breaking changes before they reached consumers [n:s3].
- That let teams evolve schemas while keeping consumers compatible [n:s3].
- We had zero consumer breakages across 140 schema changes in 18 months [n:s3].

*Grade: grounded True, invented [], spoken 4, shape 5: Accurate and well-sized, though full sentences are slightly more scripted than glanceable cues.*

## schema_changes (technical), deep pass

> I'm curious how you handled schema changes in that pipeline

I introduced a schema registry with backward-compatible Avro schemas and a CI check that blocked breaking changes; we made 140 schema changes over 18 months with zero consumer breakages. [n:s3] The tradeoff was less freedom to change schemas, but I chose that constraint to protect consumers. [n:s3]

*Grade: grounded True, invented [], spoken 3, shape 2: Dense paragraph rather than 4–6 glanceable lines, with little explanation of how the compatibility check works.*

## process_thread (technical), fast pass

> What's the difference between a process and a thread, and when would you pick one over the other?

- I’d explain that processes have separate memory, while threads share a process’s memory.
- I’d choose processes for stronger isolation or separate applications; threads for concurrent work sharing state.
- Threads need care around shared data; processes need communication between them.
- I’d weigh isolation, coordination, and workload needs before choosing.

*Grade: grounded True, invented [], spoken 4, shape 5: Good concise technical cues, though the 'I’d explain' framing and final point add mild filler.*

## process_thread (technical), deep pass

> What's the difference between a process and a thread, and when would you pick one over the other?

I’d approach that choice by asking whether the work needs to run independently or share state. I’d also ask what should happen to the rest of the service if one worker fails.

For a concrete example, I’d use my order reconciliation pipeline, which used Kafka, Go consumers, and PostgreSQL, and evaluate the worker model around those questions. [n:s2]

The tradeoff I’d focus on is isolation versus the cost of coordinating the workers.

*Grade: grounded True, invented [], spoken 3, shape 2: Discusses selection criteria but never explains process versus thread memory sharing and isolation, and uses paragraphs instead of the requested 4–6 concise lines.*

## do_differently (behavioral), fast pass

> What would you do differently if you built it again

- I’d plan shadow mode and per-region accuracy checks before launch; our first ETA release missed rural accuracy [n:s8]
- I’d use real-world validation before exposing predictions to users [n:s8]
- I’d keep the rollback path ready, then relaunch only after the checks passed [n:s8]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## do_differently (behavioral), deep pass

> What would you do differently if you built it again

**Situation:** I moved our order reconciliation from nightly batches to a streaming pipeline using Kafka, Go consumers, and PostgreSQL. [n:s2]

**Task:** I needed to reduce the lag while keeping the migration safe for existing consumers. [n:s2]

**Action:** If I built it again, I’d make schema compatibility an explicit requirement from the start. I’d use backward-compatible Avro schemas and a CI check to block breaking changes. I’d also keep the side-by-side rollout we used for six weeks. [n:s3] [n:s2]

**Result:** The original migration reduced lag from 24 hours to under two minutes. Our schema checks also supported 140 changes with zero consumer breakages over 18 months. [n:s2] [n:s3]

*Grade: grounded True, invented [], spoken 2, shape 2: Exceeds 70 words and uses scripted STAR prose rather than short cues headed by the story name, though the proposed change answers the question.*

## index (technical), fast pass

> How does a database index actually speed up a query?

- I think of an index like a lookup structure that avoids scanning every row.
- The database uses the indexed value to find matching rows, then fetches their data.
- This helps most when queries filter or join on indexed columns [n:s4].
- Indexes also cost storage and slow writes, so I’d add them based on query patterns.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## index (technical), deep pass

> How does a database index actually speed up a query?

I think of an index as a shortcut: it lets the database find matching rows without scanning the whole table. For a typical B-tree index, the database searches an ordered structure to locate the relevant entries, then retrieves the rows it needs. [n:s4]

At Tidewater, I helped identify the database as the tracking service’s bottleneck. We added two indexes and a cache, and together those changes reduced p95 latency by 70%, without a rewrite. [n:s4]

The tradeoff I’d consider is write overhead: inserts, updates, and deletes also have to maintain the indexes, so I’d choose them around the queries that matter. [n:s4]

*Grade: grounded True, invented [], spoken 2, shape 2: Accurate and grounded, but uses full prose paragraphs and exceeds 70 words instead of giving 4–6 short cue lines.*

## manager (behavioral), fast pass

> Have you ever disagreed with a decision your manager made? What happened?

- My manager wanted to rewrite the tracking service in a new language; I thought we should test first. [n:s4]
- I proposed a two-week spike with measurable success criteria. [n:s4]
- It showed the database was the bottleneck, so we fixed it with two indexes and a cache. [n:s4]
- P95 improved 70%, and we dropped the rewrite. [n:s4]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## manager (behavioral), deep pass

> Have you ever disagreed with a decision your manager made? What happened?

At Tidewater, my manager wanted to rewrite our tracking service in a new language. I disagreed because I wanted to verify what was actually causing the performance problem before committing to a rewrite. [n:s4]

My goal was to help us choose a fix based on evidence. I proposed a two-week spike with measurable criteria instead. [n:s4]

The spike showed that the database was the bottleneck. We addressed it with two indexes and a cache. [n:s4]

That brought p95 latency down by 70%. My manager agreed with the findings, and we dropped the rewrite. For me, the key was making it a shared experiment rather than an argument about whose opinion was right. [n:s4]

*Grade: grounded True, invented [], spoken 2, shape 2: Supported story, but exceeds 70 words and uses read-aloud paragraphs and reflective filler instead of 4–6 short cues starting with the story name.*

## url (technical), fast pass

> Walk me through what happens when you type a URL into the browser and hit enter.

- I’d start by checking browser cache, then resolve the hostname through DNS if needed.
- The browser opens a connection, typically TCP with TLS, or QUIC for HTTP/3.
- It sends an HTTP request; the server processes it and returns a response.
- The browser parses the response, fetches dependent assets, and renders the page.

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## url (technical), deep pass

> Walk me through what happens when you type a URL into the browser and hit enter.

I’d break it into a few steps: the browser parses the URL, resolves the hostname through DNS if needed, and establishes a connection, including TLS for HTTPS. It then sends an HTTP request, receives the response, and loads any additional resources needed to render the page.

In my work, I’ve dealt with the backend part of that path: a tracking service was slow because of database access, and adding two indexes and a cache brought p95 latency down 70%. [n:s4]

One tradeoff I’d highlight is latency versus freshness: caching can make requests faster, but I’d need to decide how much stale data is acceptable. [n:s4]

*Grade: grounded True, invented [], spoken 2, shape 2: Technically sound and grounded, but written as read-aloud paragraphs rather than 4–6 short cue lines, and exceeds 70 words.*

## leaving (behavioral), fast pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

- My team’s roadmap is shifting toward frontend work, and I want to stay focused on backend systems.
- I’m looking for larger-scale payments work and more system design ownership.
- Your streaming settlement work is especially relevant to my experience moving reconciliation to Kafka. [n:s2]

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## leaving (behavioral), deep pass

> Sorry, I lost my train of thought. Why are you looking to leave your current role?

My current team at Larkspur is shifting its roadmap toward frontend work. I want to stay focused on backend systems and take on more system design ownership. [n:a1]

I’ve owned the order pipeline and payments adapter layer. One project I’m proud of was moving reconciliation from nightly batch jobs to Kafka streaming. That brought lag down from 24 hours to under 2 minutes. [n:a1] [n:s2]

Now I’m looking for a larger-scale payments role where I can build on that experience. Your move from batch settlement to streaming is a strong match for the work I want to do next. [n:s2]

*Grade: grounded True, invented [], spoken 3, shape 2: Grounded and relevant, but full prose paragraphs exceed the 70-word limit and replace the requested 4–6 short cue lines.*

## questions_for_us (other), fast pass

> We're nearly out of time. Do you have any questions for us?

- How far along is the move from batch settlement to streaming, and what’s been hardest? [n:s2]
- How do teams divide ownership of shared payment services?
- What does on-call look like, and how are incidents reviewed?

*Grade: grounded True, invented [], spoken 5, shape 5: ok*

## questions_for_us (other), deep pass

> We're nearly out of time. Do you have any questions for us?

Yes. I’d love to understand how far along you are in the move from batch to streaming settlement, and what the hardest problem has been so far. [n:s2]

I’d also like to hear how senior engineers shape system design decisions and own shared payment services. I own the order pipeline and payments adapter layer today, so that’s especially relevant to me. [n:a1]

*Grade: grounded True, invented [], spoken 3, shape 2: Relevant questions, but at 68 words it exceeds the under-40-word limit and reads as full sentences rather than quick cues.*
