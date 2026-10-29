# Answer samples: q1029-before

All questions:

| | q1029-before fast | q1029-before deep |
|---|---|---|
| Answers graded | 6 | 6 |
| Grounded (cited node supports it) | 0.83 | 0.67 |
| No invented facts | 0.67 | 0.67 |
| Complete, 1 to 5 (grader) | 3.17 | 3.67 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 5 | 3.33 |
| Length and shape, 1 to 5 (grader) | 4.33 | 2.33 |
| Words per answer | 30.7 | 90 |
| Words per line | 8.7 | 13.8 |
| Lines with a citation | 0.5 | 0.48 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.17 | 0 |
| Full written sentences per answer | 0 | 5.33 |

Questions the profile does not cover:

| | q1029-before fast | q1029-before deep |
|---|---|---|
| Answers graded | 6 | 6 |
| Grounded (cited node supports it) | 0.83 | 0.67 |
| No invented facts | 0.67 | 0.67 |
| Complete, 1 to 5 (grader) | 3.17 | 3.67 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 5 | 3.33 |
| Length and shape, 1 to 5 (grader) | 4.33 | 2.33 |
| Words per answer | 30.7 | 90 |
| Words per line | 8.7 | 13.8 |
| Lines with a citation | 0.5 | 0.48 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0.17 | 0 |
| Full written sentences per answer | 0 | 5.33 |

## learn_fast (behavioral, not covered), fast pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- Kafka streaming, replacing nightly batch reconciliation [n:s2]
- I’d focus first on the smallest end-to-end path
- Ran old and new pipelines side by side for six weeks [n:s2]
- Lag fell from 24 hours to under two minutes [n:s2]

*Grade: grounded True, invented [], complete 2, correct 5, spoken 5, shape 4: Uses a real Kafka project but does not acknowledge the loose fit or explain learning quickly to meet a deadline.*

## learn_fast (behavioral, not covered), deep pass

> Tell me about a time you had to learn a new technology quickly to hit a deadline.

- I replaced nightly batch reconciliation with a streaming pipeline using Kafka, Go consumers, and PostgreSQL. [n:s2]
- I had to get comfortable with Kafka’s consumer behavior quickly to meet our cutover deadline. [~]
- I narrowed my learning to partition ordering, offset commits, and what happens during rebalances. [~]
- I built a small consumer and deliberately crashed it to test replay and duplicate handling. [~]
- I used those tests to check that retries wouldn’t apply the same reconciliation twice. [~]
- We ran old and new side by side for 6 weeks before cutover. [n:s2]
- Reconciliation lag went from 24 hours to under 2 minutes. [n:s2]

*Grade: grounded False, invented ['Had to learn Kafka consumer behavior quickly to meet a cutover deadline.', 'Focused learning on partition ordering, offset commits, and rebalances.', 'Built and deliberately crashed a small consumer to test replay and duplicate handling.', 'Used those tests to verify retries would not apply reconciliation twice.'], complete 3, correct 5, spoken 3, shape 2: Invents the learning-under-deadline story despite [~] markers, and exceeds the six-line, 70-word limit.*

## why_company (other, not covered), fast pass

> Why do you want to work at Northwind Payments specifically?

- Your move from batch settlement to streaming matches my Kafka reconciliation work [n:s2]
- I’m looking for a larger-scale payments platform role
- The role offers more backend and system design ownership
- How are the four squads dividing the streaming migration?

*Grade: grounded True, invented ["Northwind's role offers more backend and system design ownership; that is the candidate's goal, not an established feature of the role."], complete 4, correct 5, spoken 5, shape 4: Frame greater design ownership as something sought, not promised by Northwind; the closing question displaces another reason for joining.*

## why_company (other, not covered), deep pass

> Why do you want to work at Northwind Payments specifically?

- Your move from batch jobs to streaming is the main draw for me.
- I moved order reconciliation from nightly batch to Kafka, Go consumers, and PostgreSQL. [n:s2]
- That brought reconciliation lag from 24 hours to under 2 minutes. [n:s2]
- Your Go, Python, AWS, PostgreSQL, and Kafka stack also matches my backend skills. [n:a1]
- I want a larger-scale payments role with more system design ownership.
- My current team's roadmap is shifting toward frontend work, and I want to stay deep in backend.
- With four squads, I'd want to understand how you divide ownership of shared payment services.
- Which part of the streaming settlement move is proving hardest so far?

*Grade: grounded True, invented [], complete 5, correct 5, spoken 3, shape 2: Strong, grounded fit, but eight lines exceed the 3–5-line, under-60-word limit; drop the closing questions and tighten the full sentences into cues.*

## named_platform (technical, not covered), fast pass

> How much have you worked with Kafka specifically, beyond using it as a queue?

- Kafka as a streaming log, not just a queue [n:s2]
- Reconciliation lag: 24 hours to under 2 minutes [n:s2]
- Ran old and new pipelines side by side six weeks [n:s2]
- Schema registry, backward-compatible Avro, CI checks; 140 changes, zero breaks [n:s3]

*Grade: grounded True, invented [], complete 3, correct 5, spoken 5, shape 5: Strong real-project cues, but misses Kafka tenure, Go consumers, and concrete Kafka mechanics such as ordering, offsets, rebalancing, and duplicate handling.*

## named_platform (technical, not covered), deep pass

> How much have you worked with Kafka specifically, beyond using it as a queue?

- I replaced nightly batch reconciliation with a streaming pipeline using Kafka, Go consumers, and PostgreSQL. [n:s2]
- Reconciliation lag went from 24 hours to under 2 minutes. [n:s2]
- I introduced a schema registry with backward-compatible Avro schemas and a CI check that blocks breaking changes. [n:s3]
- I keyed order events by order ID to keep each order’s events in one partition. [~]
- I committed consumer offsets after PostgreSQL transactions completed, with duplicate detection inside the transaction. [~]
- I’d distinguish consumer-group load balancing from independent groups replaying the same retained event log.
- I’d size partitions around throughput and consumer parallelism; ordering holds within a partition, not across partitions.
- The main pitfall is treating Kafka transactions as exactly-once PostgreSQL writes; I’d still need idempotency.

*Grade: grounded False, invented ['Keyed order events by order ID to keep each order’s events in one partition.', 'Committed consumer offsets after PostgreSQL transactions completed, with duplicate detection inside the transaction.'], complete 4, correct 5, spoken 3, shape 2: Two Kafka implementation details are invented, and eight bullets exceed the requested 4–7 lines and 80-word limit.*

## years_one_domain (technical, not covered), fast pass

> And how many years have you worked on payments specifically?

- About four years on Larkspur’s checkout platform [n:a1] [~]
- Specifically, I own the order pipeline and payments adapter layer [n:a1]
- Earlier backend work was logistics, not payments [n:a2]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 5, shape 5: ok*

## years_one_domain (technical, not covered), deep pass

> And how many years have you worked on payments specifically?

- About four years specifically in payments, at Larkspur Commerce since 2022. [n:a1]
- I’m a Senior Backend Engineer on Checkout Platform, a team of nine engineers. [n:a1]
- I own the order pipeline and the payments adapter layer. [n:a1]
- Before that, I built shipment tracking APIs and the event ingestion service at Tidewater Logistics. [n:a2]
- For payments, I’d distinguish a failed request from a payment whose outcome is unknown.
- The main pitfall is retrying an ambiguous timeout and accidentally charging someone twice.

*Grade: grounded True, invented [], complete 5, correct 5, spoken 4, shape 3: Correct tenure and supported background, but the extra payment-retry advice is unrelated to a simple years-of-experience question.*

## two_part (technical, not covered), fast pass

> Thanks, that's clear. So if you had fifty services on AWS, what would you use to deploy them, and which tool do you like most for watching them in production?

- ECS on AWS, with Terraform-managed service definitions
- Per-service rolling deploys, health checks, gradual traffic shifts
- OpenTelemetry traces and metrics; CloudWatch dashboards and alarms
- Most useful: traces tied to logs, especially across Kafka flows

*Grade: grounded True, invented [], complete 4, correct 5, spoken 5, shape 5: Clear cues answer both parts, but omit a shared deployment pipeline or template and explicit rollback.*

## two_part (technical, not covered), deep pass

> Thanks, that's clear. So if you had fifty services on AWS, what would you use to deploy them, and which tool do you like most for watching them in production?

- I’d deploy fifty services on ECS Fargate, with Terraform defining infrastructure and service configuration.
- I’d use immutable ECR images, task definitions, and rolling deployments with deployment circuit breaker rollback.
- I’d give each service its own IAM task role, health checks, and autoscaling policies.
- For watching production, I’d favor Grafana, with OpenTelemetry connecting metrics and traces across services.
- I’d track request rate, errors, latency, saturation, and Kafka consumer lag for streaming workloads.
- I’d alert on customer impact and link dashboards to traces, rather than paging on every spike.
- The tradeoff is Fargate’s cost versus less cluster management; telemetry also needs cardinality limits.

*Grade: grounded True, invented [], complete 4, correct 5, spoken 4, shape 3: Answers both parts well, but exceeds the 80-word limit and omits a shared deployment pipeline or service template.*

## worked_here_before (other, not covered), fast pass

> Have you ever worked for Northwind Payments before, as an employee or a contractor?

- No, I haven’t worked for Northwind Payments before
- What would you like to know about my background?

*Grade: grounded False, invented ['The candidate has never worked for Northwind Payments.'], complete 1, correct 5, spoken 5, shape 3: The notes invent a definitive no where the profile leaves the candidate to answer.*

## worked_here_before (other, not covered), deep pass

> Have you ever worked for Northwind Payments before, as an employee or a contractor?

- I’m currently Senior Backend Engineer at Larkspur Commerce, on the Checkout Platform team. [n:a1]
- Before that, I was a Backend Engineer at Tidewater Logistics, from 2018 to 2022. [n:a2]
- My earlier role was a Software Engineer intern at Brightfield Labs, summer 2017. [n:a3]

*Grade: grounded True, invented [], complete 1, correct 5, spoken 3, shape 2: Lists supported employment history but evades the question; leave the yes/no to the candidate rather than reciting their résumé.*
