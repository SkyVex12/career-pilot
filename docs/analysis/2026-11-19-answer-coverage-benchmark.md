# Sensei benchmark — 2026-11-19

## Status and scope

Answer quality is the primary outcome. **Provisional model grades.** At the user's request, 15/15 samples received independent AI review instead of human scoring. This is AI-only agreement; human calibration remains unvalidated. Results describe the selected recorded run.

99 exported suggestions over 45 minutes 13 seconds; 35 turns originally labelled as requiring a response by a question-only annotator. The primary candidate-question scope contains 33 turns. Original labels are retained below as a sensitivity result; all exported turns still entered the replay.

Independent addressee audit: gpt-6.2-astra; 32 candidate, 2 other, 1 unclear. It used interviewer context only, without either assistant's answers or grades. Only explicit other addressees are excluded; unclear cases remain in the primary scope. Excluded turn IDs: [3, 8]. Eligibility remains model-adjudicated and provisional.

Sergio was not present in the copilot. The supplied three-page resume and TurnCare Full Stack Developer JD were used to create a fresh, isolated benchmark pairing. The actual worker extraction, node validation, digest renderer and interview pack builder were used with in-memory storage. Preparation used gpt-6.1-sol instead of the locally configured, unpriced gpt-6-sol extraction route, and a longer provider timeout after a pack timeout. This is not a historical reconstruction of an existing pack.

Replay used the actual gateway with its configured detector, gpt-6-luna fast model, gpt-6.1-sol deep model and light-deep routing, prepared-answer embeddings, cancellation and conversation memory. Local storage and text STT replaced infrastructure and audio. Only interviewer turns entered the gateway; it never received Sensei suggestions or candidate speech. Consequently memory builds from interviewer turns and app answers, not from the candidate's missing spoken responses.

Replay speed: 1.0x. Export timestamp semantics are unknown: timing is measured from text injection, **not verified question end**. STT, speech duration and overlay render time are excluded. Sensei generation latency is unavailable. The exported sample contains only turns shown to Sensei, so its observed coverage is 100% by construction; this does not establish its recall over the full interview.

Sensei's original model, prompts, profile version and conversation context were not independently reconstructed or verified. This compares historical exported suggestions with our controlled replay, judged against the supplied resume/JD; it does not isolate answer engines under identical upstream conditions.

## Scoring

Independent judge: gpt-6-astra; six equally weighted 1-5 criteria. Seed 20261119; each turn judged twice in reversed A/B order. The judge sees source resume/JD evidence and anonymized answers, not product names. Citation markers are removed symmetrically. The configured Anthropic credential failed a metadata check, so GPT-6 Astra was used. It differs from both generators but shares their provider family; writing style can still reveal provenance. Source text is data, not instructions. No new Sensei answers were generated.

Initial = our fast answer versus the exported Sensei suggestion. Expanded = our deep answer when produced, otherwise fast; Sensei uses the same exported suggestion. Empty answers score 1 in the coverage-inclusive tables below. Conditional scores exclude empty answers. No best-of-pass cherry-picking.

| Criterion | Our initial | Our expanded/fast fallback | Sensei |
| --- | ---: | ---: | ---: |
| relevance | 4.11 | 4.32 | 3.52 |
| grounding | 4.62 | 4.39 | 2.32 |
| specificity | 3.59 | 4.05 | 2.27 |
| structure | 4.17 | 4.29 | 2.58 |
| speakability | 4.53 | 4.32 | 1.62 |
| first_two_lines | 4.47 | 4.41 | 1.64 |
| **Overall** | 4.25 | 4.30 | 2.32 |

Conditional initial quality: 4.35 versus Sensei 2.33 on the same 32 questions. Conditional deep quality: 4.35 (n=28).

| Question type | N | Our initial | Our expanded | Sensei |
| --- | ---: | ---: | ---: | ---: |
| behavioral | 16 | 4.05 | 4.10 | 2.12 |
| respond | 1 | 4.83 | 4.83 | 2.83 |
| small_talk | 2 | 4.42 | 4.42 | 3.62 |
| technical | 14 | 4.41 | 4.46 | 2.33 |

| Pairwise result | Win | Tie | Loss | Order disagreement |
| --- | ---: | ---: | ---: | ---: |
| preference | 31 (93.9%) | 0 (0.0%) | 2 (6.1%) | 0 (0.0%) |
| expanded_preference | 31 (93.9%) | 0 (0.0%) | 2 (6.1%) | 0 (0.0%) |

Order disagreements remain unresolved; they are not counted as ties.

### Eligibility sensitivity

The original denominator remains visible to show the effect of addressee adjudication. Excluding another participant's question does not change any generated answer or grade.

| Scope | N | Detections | Initial outputs | Our initial | Our expanded | Sensei | Initial win / tie / loss / disagreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Primary candidate scope | 33 | 32 | 32 | 4.25 | 4.30 | 2.32 | 31 / 0 / 2 / 0 |
| Original response-seeking labels | 35 | 32 | 32 | 4.06 | 4.11 | 2.33 | 32 / 0 / 3 / 0 |

## Coverage and timing

Confirmed/visible detections: 32/33. Initial answers: 32/33; deep answers: 28/33.
Coverage uses strict exported-turn card attribution, not semantic-question recall. Nearby turns can continue or merge one question (potential cases include 30/31 and 35/36), so a missing distinct card does not by itself establish an omitted semantic question. Answers and grades are never reused across those source turns.
Cards on turns labelled non-response-seeking: 12/64. These are provisional false-positive candidates, not confirmed errors.

| Stage, seconds from injection | p50 | p95 | N |
| --- | ---: | ---: | ---: |
| detected_s | 0.53 | 1.92 | 32 |
| fast_first_s | 1.22 | 1.92 | 32 |
| deep_first_s | 7.61 | 11.00 | 28 |
| fast_done_s | 1.84 | 2.34 | 32 |
| deep_done_s | 7.72 | 11.08 | 28 |

Initial answers after a predeclared 2-second injection deadline: 1. Missed answers are counted separately, never assigned a latency of zero.
Answers first appearing after the next response-seeking turn: 0. This uses recorded spacing, without assuming that timestamps mark question ends.

## Ten largest initial-answer deficits

Ranked by equal-weight score gap, not selected anecdotes. Categories below are diagnostic hypotheses unless directly established by events. Retrieval versus missing memory requires checking the private profile/pack evidence; scores alone cannot distinguish those causes.

| Turn | Paraphrased intent | Score deficit | Classification | Judge explanation |
| --- | --- | ---: | --- | --- |
| 35 | Engineers may need to negotiate requirements with product and other teams, represent engineering’s perspective, and raise constraints. How would you handle that? | 1.17 | not detected (observed) | The implicit request about negotiating scope and representing engineering constraints produced neither a detection nor an answer. |
| 0 | Exchange greetings and ask how the other person is doing. | 0.08 | prompt/shape (observed) | The greeting reciprocates politely but never answers the interviewer's wellbeing check, making the exchange slightly mechanical. |

## Answer-quality deficits where an answer appeared

This separates weak answers from missed detection. Rows are ranked on the initial score gap on matched questions; zero or negative gaps are omitted.

| Turn | Paraphrased intent | Initial deficit | Expanded deficit | Judge explanation |
| --- | --- | ---: | ---: | --- |
| 0 | Exchange greetings and ask how the other person is doing. | 0.08 | 0.08 | B directly answers the greeting and reciprocates warmly; A is concise but skips answering how things are going. |

### Absolute quality risks in delivered answers

Relative wins can still have weak scores or unsupported personal claims. These rows include delivered answers below 4/5 overall or with a nonzero claim finding in either order. They are ranked by our initial score, not by a Sensei loss. Claim counts show the two judging orders separately.

| Turn | Paraphrased intent | Our initial / expanded | Initial claims | Expanded claims |
| --- | --- | ---: | --- | --- |
| 48 | Do you have much experience working with AWS? | 3.17 / 3.33 | [0, 0] | [0, 0] |
| 33 | Are you comfortable working on a small team at a small company? | 3.42 / 3.67 | [0, 0] | [1, 2] |
| 92 | Describe a time you worked with cross-functional teams to reach a shared goal. How did you make it happen? | 3.50 / 3.67 | [1, 1] | [0, 0] |
| 26 | Asks about the candidate’s experience with small teams that build new products and maintain and iterate on deployed ones. | 3.75 / 3.83 | [1, 1] | [1, 1] |
| 30 | When planning and building a product or feature, how do you ensure the design supports measuring its success after launch? | 3.83 / 4.33 | [3, 3] | [0, 0] |
| 68 | Have you used Epic itself, or only worked with data exported from it? | 3.92 / 3.83 | [0, 0] | [1, 0] |
| 70 | Are you familiar with the HL7 protocol and its data format, even though you do not currently use FHIR? | 3.92 / 4.00 | [0, 0] | [0, 0] |
| 93 | What is one area you are working to improve, either professionally or personally? | 4.08 / 3.75 | [0, 0] | [1, 1] |
| 27 | Can you explain in more detail how you built healthcare data workflows that integrated with hospital EMR systems, including how you accessed EMR data and used it in those workflows? | 4.33 / 3.67 | [0, 0] | [2, 3] |
| 86 | Describe your experience writing effective queries for established SQL databases such as MySQL, and designing schemas or indexes. | 4.33 / 4.25 | [0, 0] | [1, 1] |
| 89 | Which of these workplace values—empowering people, continuous improvement, teamwork, compassion, excellence, or passionate service—resonates most with you, and why? | 4.33 / 4.33 | [0, 0] | [1, 0] |
| 55 | The interviewer confirms the candidate is in Central Time, notes the team is geographically distributed, and asks whether the candidate has questions about the product and company. | 4.50 / 4.50 | [1, 1] | [1, 1] |
| 91 | What interests you about this role, and what drew you to it? | 4.50 / 3.83 | [0, 0] | [1, 1] |
| 54 | Or do you build from scratch? Describe your usual approach. | 4.75 / 5.00 | [1, 0] | [0, 0] |

## Strengths to preserve

- Turn 18: When someone on the product team asks you to create something, what happens in that process? — Centers the proposed process on user outcomes, data definitions, and validation, while keeping the documented dashboard example separate from prospective working practices.
- Turn 19: For an internal tool or a polished, customer-facing product, what documentation, tickets, design mockups, or other artifacts do you typically provide? — Provides concrete artifact contents, distinguishes internal-tool sketches from customer-facing design detail, and includes testable data and interface edge cases without inventing tool usage.
- Turn 20: What do you need from a workplace to do your best work? — Maintains the surrounding discussion's focus on development inputs instead of drifting into generic workplace benefits, and allows requirements to be refined collaboratively.
- Turn 21: What level of guidance works best for you, from detailed requirements and polished mockups to broad goals or rough sketches? — Directly addresses the required level of guidance: rough goals and sketches can suffice when decision ownership, feedback, and validation criteria are available.
- Turn 22: What would help you feel comfortable and effective in the role? — Connects effective execution to practical data access and expected results while preserving flexibility about implementation details.
- Turn 24: What experience do you have maintaining a product over the long term after requirements, development, and deployment? — Uses documented device diagnostics, outage fallback, and release testing to support a maintenance discussion, clearly distinguishing proposed future practices from past contributions.
- Turn 26: Asks about the candidate’s experience with small teams that build new products and maintain and iterate on deployed ones. — Despite the coverage and grounding risks, supplies concrete, resume-supported reliability mechanisms and customer-facing features rather than the competitor's unsupported architecture and feedback-loop history.
- Turn 27: Can you explain in more detail how you built healthcare data workflows that integrated with hospital EMR systems, including how you accessed EMR data and used it in those workflows? — The initial answer accurately links clinical data access, ingestion, and processing to utilization-review decisions without inventing EMR vendors or authentication mechanisms; the expansion is less consistently grounded.
- Turn 30: When planning and building a product or feature, how do you ensure the design supports measuring its success after launch? — Offers a practical delivery approach covering integration uncertainty, scope changes, acceptance checks, and production validation without relying on unsupported project-management tools or ceremonies.
- Turn 31: How do you design for assessing whether a deployed solution is successful? — Connects measurable outcomes to instrumentation, baselines, and user feedback, with useful safeguards for sensitive telemetry and missing observations; proposed practices remain distinct from documented experience.

## Improvement plan — approval required before implementation

The priorities are provisional until calibration. Expected gain is a testable hypothesis, not a measured improvement. Effort estimates are relative.

The user waived human scoring. The separate AI review and addressee audit support consistency checks, while priorities remain provisional without human calibration. Opening pleasantries require an addressee/usefulness check before adding triggers. The ranked experiments below use an explicit planning assumption: recovering 25% of each observed deficit. The estimated gain is on the overall 1-5 initial score, divided by relative effort (small=1, medium=3, large=8) for ordering. These estimates are not measured effects, overlap across experiments, and must not be summed. For absolute-quality follow-ups that are not relative losses, the same 25% assumption applies to headroom between our initial score and 5; those entries are explicitly labelled and do not claim a measured Sensei deficit.

| Turn | Proposed experiment | Loss reason | Assumed score gain / effort |
| --- | --- | --- | --- |
| 48 | Test experience answers that first distinguish source-supported history from unverified platform experience, then offer one transferable example and a clearly prospective approach. | prompt/shape | +0.014 / small (absolute quality; headroom to 5) |
| 33 | Test a claim check that blocks unsupported team-size assertions while retaining documented full-stack breadth and conditional small-team working preferences. | prompt/shape | +0.012 / small (absolute quality; headroom to 5) |
| 92 | Test a behavioral-answer check separating product capabilities from the candidate's interpersonal actions; flag absent collaboration details rather than inventing meetings or agreements. | too generic | +0.011 / medium (absolute quality; headroom to 5) |
| 26 | Replay the merged turn with a two-part coverage check for new development and ongoing reliability, requiring source support for each historical claim. | prompt/shape | +0.009 / medium (absolute quality; headroom to 5) |
| 35 | Replay fragmented stakeholder-negotiation turns with an implicit-request detector; measure recall and false triggers on ordinary interviewer commentary. | not detected | +0.009 / medium |
| 30 | Compare incremental expanded-answer delivery with current buffering; measure first-visible latency and how often expansions arrive after a follow-up begins. | too slow | +0.009 / medium (absolute quality; headroom to 5) |
| 0 | Investigate addressee and cue usefulness before any trigger implementation. Test a one-sentence greeting format that acknowledges wellbeing before reciprocating, without bullets. | prompt/shape | +0.001 / small |

Every proposed change must compare per-criterion and per-type scores, agreement, unsupported claims, coverage and win regressions on the same evidence. Implemented changes require a separate before/after comparison.

## Calibration and reproducibility

The private human-review.html/JSON contains 15 seeded random questions from the original actionable-turn labels, with A/B answers and score fields. The user requested AI scoring instead. gpt-6.2-astra independently graded the same initial-answer sample once, with no primary grades or product labels. The table measures AI-to-AI agreement, not human calibration. Both reviewers share a provider family, so correlated biases remain possible. The original human trust threshold was 15 complete, preference agreement >=80%, and each criterion's mean absolute error <=0.75. Meeting that numerical threshold with AI scores does not satisfy the human trust gate.

Run the command in evals/README.md with the external export and private artifacts. Resume/pack construction is cached for comparable before/after runs. The committed fixture contains paraphrased questions only; original wording and Sensei answers are required locally to reproduce the original benchmark. A paraphrase-only regression run is a distinct experiment, not an exact reproduction.

Calibration status: **ai_agreement_only**. Completed 15/15. Preference agreement: 100.0%.

| Calibration criterion | Mean absolute error |
| --- | ---: |
| relevance | 0.417 |
| grounding | 0.233 |
| specificity | 0.333 |
| structure | 0.167 |
| speakability | 0.183 |
| first_two_lines | 0.483 |

Review covers initial answers only; expanded grades remain model-only. Human reviews completed: 0/15.

## Spend

Usage-accounted calls: $4.185551. Conservative unresolved-call/embedding reservations: $6.275322. **Combined conservative ledger: $10.460873.** This is the selected run directory's ledger only. Calls from other run directories, including earlier aborted attempts or reused preparation, are not included unless their entries were explicitly carried into this ledger. Reservations are conservative estimates for unresolved usage, not confirmed billed charges. No new billed infrastructure. Usage-accounted amounts apply configured token prices; cache-write premiums and billing adjustments are not exposed by the live adapter. The combined ledger is an estimate with unresolved allowances, not an invoice or a guaranteed billing upper bound.

Prices checked against [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). The ledger covers the setup, embedding, generation and review calls metered in this run. Reusing evidence or labels does not copy the costs of their earlier preparation. Independent reviewer rates use the [GPT-6.2 Astra model page](https://developers.openai.com/api/docs/models/gpt-6.2-astra).

| Model | Calls | Usage-accounted USD | Unresolved allowance USD |
| --- | ---: | ---: | ---: |
| gpt-6-astra | 71 | 3.580140 | 0.000000 |
| gpt-6-luna | 217 | 0.033933 | 0.228132 |
| gpt-6.1-sol | 45 | 0.062678 | 6.031947 |
| gpt-6.2-astra | 15 | 0.508800 | 0.000000 |
| text-embedding-3-large | 99 | 0.000000 | 0.015243 |

## Unsupported personal claims

Counts are judge findings against the supplied resume/JD, not independently verified fabrications. A claim absent from these sources may still be true. The two order counts are shown separately so disagreement is visible; they are not added together.

| Side and answer | Order 1 total | Order 2 total |
| --- | ---: | ---: |
| ours initial | 7 | 6 |
| ours expanded | 10 | 10 |
| sensei initial | 155 | 159 |
| sensei expanded | 155 | 159 |

## Per-question audit

Scores average six criteria across both orders. Claim counts are initial-order pairs. Stage timings, answer text and source references remain in private answers.json.

| Turn | Expected / detected type | Our initial / expanded | Sensei | Preference | Claims ours / Sensei | Fast first / deep first (s) | Large score disagreement |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| 0 | small_talk / small_talk | 4.33 / 4.33 | 4.42 | sensei | [0, 0] / [0, 0] | 2.12 / missing | False |
| 18 | technical / other | 4.67 / 4.75 | 2.00 | ours | [0, 0] / [1, 1] | 1.91 / 9.50 | False |
| 19 | technical / other | 4.50 / 4.92 | 1.75 | ours | [0, 0] / [8, 8] | 1.91 / 10.45 | False |
| 20 | behavioral / technical | 4.50 / 5.00 | 2.33 | ours | [0, 0] / [0, 0] | 0.78 / 9.66 | False |
| 21 | behavioral / behavioral | 4.58 / 5.00 | 2.83 | ours | [0, 0] / [0, 0] | 0.94 / 9.78 | False |
| 22 | behavioral / behavioral | 4.58 / 5.00 | 2.25 | ours | [0, 0] / [0, 0] | 1.67 / 11.08 | False |
| 24 | behavioral / behavioral | 4.42 / 4.25 | 1.83 | ours | [0, 0] / [5, 5] | 1.34 / 9.78 | False |
| 26 | behavioral / behavioral | 3.75 / 3.83 | 1.92 | ours | [1, 1] / [6, 7] | 1.42 / 3.59 | False |
| 27 | technical / behavioral | 4.33 / 3.67 | 2.08 | ours | [0, 0] / [9, 9] | 1.30 / 9.19 | False |
| 30 | behavioral / other | 3.83 / 4.33 | 2.00 | ours | [3, 3] / [7, 7] | 1.92 / 10.33 | False |
| 31 | behavioral / system_design | 4.67 / 4.33 | 2.17 | ours | [0, 0] / [7, 7] | 1.22 / 10.48 | False |
| 32 | technical / technical | 4.50 / 4.83 | 1.83 | ours | [0, 0] / [8, 8] | 1.39 / 10.06 | False |
| 33 | behavioral / behavioral | 3.42 / 3.67 | 1.83 | ours | [0, 0] / [8, 8] | 0.84 / 9.86 | False |
| 35 | behavioral / not detected | 1.00 / 1.00 | 2.17 | sensei | [0, 0] / [3, 3] | missing / missing | False |
| 36 | behavioral / behavioral | 4.50 / 4.50 | 1.67 | ours | [0, 0] / [11, 12] | 1.55 / 2.78 | False |
| 37 | behavioral / respond | 4.33 / 4.33 | 1.58 | ours | [0, 0] / [8, 10] | 1.25 / 3.03 | False |
| 39 | technical / technical | 4.67 / 4.92 | 2.33 | ours | [0, 0] / [1, 1] | 0.89 / 6.95 | False |
| 40 | behavioral / technical | 4.75 / 4.75 | 1.75 | ours | [0, 0] / [8, 8] | 1.06 / 7.61 | False |
| 41 | technical / technical | 4.83 / 4.83 | 3.00 | ours | [0, 0] / [0, 0] | 1.00 / missing | False |
| 48 | technical / technical | 3.17 / 3.33 | 2.17 | ours | [0, 0] / [6, 6] | 0.56 / 7.69 | False |
| 52 | technical / other | 4.92 / 4.92 | 2.58 | ours | [0, 0] / [2, 3] | 1.92 / missing | False |
| 53 | technical / technical | 4.83 / 5.00 | 2.50 | ours | [0, 0] / [4, 4] | 0.94 / 6.80 | False |
| 54 | technical / technical | 4.75 / 5.00 | 2.83 | ours | [1, 0] / [3, 2] | 1.52 / 5.92 | False |
| 55 | small_talk / other | 4.50 / 4.50 | 2.83 | ours | [1, 1] / [0, 0] | 0.78 / 3.06 | False |
| 66 | technical / technical | 4.42 / 4.25 | 2.83 | ours | [0, 0] / [4, 4] | 0.72 / 2.84 | False |
| 68 | technical / technical | 3.92 / 3.83 | 2.50 | ours | [0, 0] / [5, 5] | 1.28 / 2.66 | False |
| 70 | technical / technical | 3.92 / 4.00 | 1.92 | ours | [0, 0] / [7, 6] | 0.77 / 2.84 | False |
| 86 | technical / technical | 4.33 / 4.25 | 2.33 | ours | [0, 0] / [6, 5] | 1.72 / 6.95 | False |
| 89 | behavioral / other | 4.33 / 4.33 | 2.25 | ours | [0, 0] / [11, 11] | 0.70 / 11.00 | False |
| 91 | behavioral / other | 4.50 / 3.83 | 2.58 | ours | [0, 0] / [1, 1] | 0.45 / 10.05 | False |
| 92 | behavioral / behavioral | 3.50 / 3.67 | 2.50 | ours | [1, 1] / [10, 11] | 0.89 / 2.88 | False |
| 93 | behavioral / behavioral | 4.08 / 3.75 | 2.25 | ours | [0, 0] / [6, 7] | 0.75 / 2.70 | False |
| 95 | respond / other | 4.83 / 4.83 | 2.83 | ours | [0, 0] / [0, 0] | 1.86 / missing | False |
