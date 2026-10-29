# Answer samples: q1029-years

All questions:

| | q1029-years fast | q1029-years deep |
|---|---|---|
| Answers graded | 2 | 2 |
| Grounded (cited node supports it) | 1.0 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Complete, 1 to 5 (grader) | 4.5 | 5 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.5 | 4 |
| Length and shape, 1 to 5 (grader) | 4 | 4 |
| Words per answer | 29.5 | 67.5 |
| Words per line | 8.2 | 11.2 |
| Lines with a citation | 0.75 | 0.92 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 6 |

Questions the profile does not cover:

| | q1029-years fast | q1029-years deep |
|---|---|---|
| Answers graded | 2 | 2 |
| Grounded (cited node supports it) | 1.0 | 1.0 |
| No invented facts | 1.0 | 1.0 |
| Complete, 1 to 5 (grader) | 4.5 | 5 |
| Technically correct, 1 to 5 (grader) | 5 | 5 |
| Sounds spoken, 1 to 5 (grader) | 4.5 | 4 |
| Length and shape, 1 to 5 (grader) | 4 | 4 |
| Words per answer | 29.5 | 67.5 |
| Words per line | 8.2 | 11.2 |
| Lines with a citation | 0.75 | 0.92 |
| Citations of unknown ids (count) | 0 | 0 |
| Stock phrases per answer | 0 | 0 |
| Full written sentences per answer | 0 | 6 |

## years_one_domain (technical, not covered), fast pass

> And how many years have you worked on payments specifically?

- About four years at Larkspur’s checkout platform, owning payments adapters [n:a1]
- Payments work centers on the order pipeline and provider integrations [n:a1]
- I’d count that checkout-platform work as my payments experience
- How do you define payments work across the team?

*Grade: grounded True, invented [], complete 4, correct 5, spoken 4, shape 4: Answers four years correctly, but repeats the scope and adds an unnecessary clarifying question instead of distinguishing the earlier logistics role.*

## years_one_domain (technical, not covered), deep pass

> And how many years have you worked on payments specifically?

- About four years, at Larkspur Commerce, from 2022 to now. [n:a1]
- I'm a Senior Backend Engineer on the Checkout Platform team, with nine engineers. [n:a1]
- I own the order pipeline and the payments adapter layer. [n:a1]
- That includes moving order reconciliation from nightly batch to Kafka streaming. [n:s2]
- Reconciliation lag went from 24 hours to under 2 minutes. [n:s2]
- Before Larkspur, my work was shipment tracking APIs and event ingestion at Tidewater. [n:a2]

*Grade: grounded True, invented [], complete 5, correct 5, spoken 4, shape 4: Accurate and well grounded, but team size and reconciliation metrics add unnecessary detail to a simple tenure question.*
