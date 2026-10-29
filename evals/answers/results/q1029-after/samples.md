# Answer samples: q1029-after

All questions:

| | q1029-after fast | q1029-after deep |
|---|---|---|
| Answers graded | 6 | 6 |
| Grounded (cited node supports it) | 0.67 | 0.83 |
| No invented facts | 0.83 | 0.83 |
| Complete, 1 to 5 (grader) | 3 | 4.17 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.83 | 3.5 |
| Length and shape, 1 to 5 (grader) | 4.17 | 3 |
| Words per answer | 31.7 | 84.7 |
| Words per line | 9.1 | 13.8 |
| Lines with a citation | 0.54 | 0.33 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.17 | 0 |
| Full written sentences per answer | 0 | 5 |

Questions the profile does not cover:

| | q1029-after fast | q1029-after deep |
|---|---|---|
| Answers graded | 6 | 6 |
| Grounded (cited node supports it) | 0.67 | 0.83 |
| No invented facts | 0.83 | 0.83 |
| Complete, 1 to 5 (grader) | 3 | 4.17 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.83 | 3.5 |
| Length and shape, 1 to 5 (grader) | 4.17 | 3 |
| Words per answer | 31.7 | 84.7 |
| Words per line | 9.1 | 13.8 |
| Lines with a citation | 0.54 | 0.33 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.17 | 0 |
| Full written sentences per answer | 0 | 5 |

## learn_fast (behavioral, not covered), fast pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- Order reconciliation: moved nightly batch processing to Kafka streaming [n:s2]
- I ran old and new pipelines side by side for six weeks [n:s2]
- I’d ramp up on unfamiliar APIs through a small end-to-end slice first
- Then validate behavior under load before committing to cutover

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 4: Uses a real adjacent project but does not acknowledge the missing deadline-learning story or explain enough of the learning approach.*

## learn_fast (behavioral, not covered), deep pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- I replaced nightly batch reconciliation with a streaming pipeline using Kafka, Go consumers, and PostgreSQL. [n:s2]
- I had to get comfortable with Kafka quickly to meet our cutover deadline. [~]
- I focused first on consumer groups, offset commits, and what happens when consumers restart. [~]
- I built a small test consumer and deliberately crashed it to check replay behavior. [~]
- I used idempotent database writes so replaying an event wouldn't apply the same change twice. [~]
- We ran old and new side by side for 6 weeks before cutover. [n:s2]
- Reconciliation lag went from 24 hours to under 2 minutes. [n:s2]
- My takeaway is to learn the failure modes first, not just the happy-path API.

*Grade: grounded False, invented ['Had to learn Kafka quickly to meet a cutover deadline.', 'Focused learning on consumer groups, offset commits, and consumer restarts.', 'Built and deliberately crashed a test consumer to check replay behavior.', 'Used idempotent database writes to prevent duplicate changes during replay.'], complete 3, correct 5, spoken 3, shape 2: Invents deadline-driven learning and implementation details; [~] does not justify them, and eight lines exceed the requested length.*

## why_company (other, not covered), fast pass

> Why do you want to work at Northwind Payments specifically?

- Northwind’s batch-to-streaming move matches my Kafka migration work [n:s2]
- Larkspur’s checkout platform gave me payments adapter ownership [n:a1]
- I want larger-scale payments and more system design ownership
- How are the four squads splitting the streaming work?

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 4: Strong fit and goals cues, but the closing question uses a slot that could strengthen why Northwind specifically.*

## why_company (other, not covered), deep pass

> Why do you want to work at Northwind Payments specifically?

- Your move from batch jobs to streaming is the main draw for me.
- I moved order reconciliation from nightly batch to Kafka streaming at Larkspur. [n:s2]
- That brought reconciliation lag from 24 hours to under 2 minutes. [n:s2]
- I want a larger-scale payments role with more system design ownership.
- My current team's roadmap is shifting to frontend work; I want to stay deep in backend.
- The Go, PostgreSQL, Kafka, and AWS stack also fits my background. [n:s2] [n:s7]
- How far along is the streaming migration, and what's been hardest so far?
- How do the four squads split ownership of shared payment services?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 4, shape 2: Strong, grounded company fit, but eight lines exceed the 3–5-line, under-60-word limit; drop the unsolicited questions and combine the migration details.*

## named_platform (technical, not covered), fast pass

> How much have you worked with Kafka specifically, beyond using it as a queue?

- Four years with Kafka; moved reconciliation to streaming [n:s2]
- Go consumers wrote events into PostgreSQL
- Ran batch and streaming side by side for six weeks [n:s2]
- Added Avro compatibility checks; 140 schema changes, no breaks [n:s3]

*Grade: grounded False, invented [], complete 3, correct 5, spoken 5, shape 5: Good project cues, but missing Kafka ordering, consumer groups, offset handling and duplicate pitfalls; the Go/PostgreSQL point also needs a citation.*

## named_platform (technical, not covered), deep pass

> How much have you worked with Kafka specifically, beyond using it as a queue?

- I have four years with Kafka, including replacing nightly batch reconciliation with a streaming pipeline. [n:s2]
- That used Kafka, Go consumers, and PostgreSQL; reconciliation lag went from 24 hours to under 2 minutes. [n:s2]
- I introduced backward-compatible Avro schemas and a CI check; zero consumer breakages after 140 schema changes. [n:s3]
- I’d choose partition keys around ordering requirements; Kafka guarantees ordering within partitions, not across topics.
- I’d use consumer groups for parallelism and retained offsets for replay, rather than destructive queue reads.
- The main pitfall is duplicate side effects: I’d make PostgreSQL writes idempotent before committing consumer offsets.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 3, shape 3: Solid, grounded answer, but exceeds 80 words, reads as full sentences rather than cues, and omits rebalancing and producer idempotence or transactions.*

## years_one_domain (technical, not covered), fast pass

> And how many years have you worked on payments specifically?

- Five years at Larkspur on the checkout platform [n:a1]
- I own the order pipeline and payments adapter layer [n:a1]
- Before that, shipment tracking and event ingestion at Tidewater [n:a2]

*Grade: grounded False, invented ['Five years at Larkspur; the supported payments tenure is about four years, from 2022 to 2026.'], complete 3, correct 5, spoken 5, shape 5: Direct, usable cues, but the payments tenure is overstated by one year.*

## years_one_domain (technical, not covered), deep pass

> And how many years have you worked on payments specifically?

- I’ve worked on payments specifically since 2022, on Larkspur Commerce’s Checkout Platform. [n:a1]
- I’m a Senior Backend Engineer, owning the order pipeline and payments adapter layer. [n:a1]
- Before that, I built shipment tracking APIs and event ingestion at Tidewater Logistics. [n:a2]
- My payments work includes moving order reconciliation from nightly batch to Kafka streaming. [n:s2]
- That brought reconciliation lag from 24 hours to under 2 minutes. [n:s2]
- The main pitfall I’d watch for is treating a payment timeout as a definite failure.
- I’d use idempotency keys and reconciliation to resolve uncertain outcomes without duplicate charges.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 3, shape 3: Supported facts, but say about four years directly and omit the unrelated timeout and idempotency discussion.*

## two_part (technical, not covered), fast pass

> Thanks, that's clear. So if you had fifty services on AWS, what would you use to deploy them, and which tool do you like most for watching them in production?

- ECS on AWS, with Terraform managing service infrastructure [n:a1]
- GitHub Actions building images and deploying by environment [~]
- OpenTelemetry traces and metrics, CloudWatch for AWS signals [n:a1]
- I like Grafana dashboards; page on actionable SLO alerts [~]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Answers both parts clearly, but omits a shared pipeline/service template, health checks, and rollback.*

## two_part (technical, not covered), deep pass

> Thanks, that's clear. So if you had fifty services on AWS, what would you use to deploy them, and which tool do you like most for watching them in production?

- I’d deploy those services on ECS with Fargate, using Terraform for the infrastructure.
- My AWS experience includes ECS, RDS, S3, and SQS; Terraform is also in my stack.
- I’d give each service its own task definition, IAM task role, and autoscaling policy.
- I’d deploy immutable images through CI, with health checks and automatic rollback on failed deployments.
- For watching production, I’d pick Grafana, with OpenTelemetry connecting metrics and traces across services.
- I’d alert on error rates, latency, and consumer lag, rather than CPU alone.
- The main pitfalls are uncontrolled telemetry cardinality and treating healthy containers as healthy payment flows.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 3, shape 3: Answers both parts well, but exceeds 80 words, uses full sentences rather than glanceable cues, and leaves the shared service template and telemetry backend implicit.*

## worked_here_before (other, not covered), fast pass

> Have you ever worked for Northwind Payments before, as an employee or a contractor?

- Did I hear whether contractors count as prior employment too?
- Were you asking about any Northwind engagement, not just employment?

*Grade: grounded True, invented [], complete 2, correct 5, spoken 4, shape 2: Two redundant clarifying questions avoid the candidate's answer even though the interviewer explicitly included contractor work.*

## worked_here_before (other, not covered), deep pass

> Have you ever worked for Northwind Payments before, as an employee or a contractor?

- Does that also include work through a third-party vendor, rather than a direct contract?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*
