# Sensei benchmark — 2026-11-19

## Status and scope

Answer quality is the primary outcome. **Provisional model grades.** At the user's request, 15/15 samples received independent AI review instead of human scoring. AI agreement does not establish human validity. No production prompts or answer code changed.

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
| relevance | 2.97 | 3.20 | 3.59 |
| grounding | 3.42 | 3.36 | 2.30 |
| specificity | 2.67 | 3.03 | 2.32 |
| structure | 3.21 | 3.20 | 2.58 |
| speakability | 3.56 | 3.33 | 1.74 |
| first_two_lines | 3.26 | 3.18 | 1.67 |
| **Overall** | 3.18 | 3.22 | 2.37 |

Conditional initial quality: 4.27 versus Sensei 2.25 on the same 22 questions. Conditional deep quality: 4.33 (n=22).

| Question type | N | Our initial | Our expanded | Sensei |
| --- | ---: | ---: | ---: | ---: |
| behavioral | 16 | 3.86 | 3.88 | 2.13 |
| respond | 1 | 1.00 | 1.00 | 2.83 |
| small_talk | 2 | 2.92 | 2.88 | 3.75 |
| technical | 14 | 2.60 | 2.67 | 2.40 |

| Pairwise result | Win | Tie | Loss | Order disagreement |
| --- | ---: | ---: | ---: | ---: |
| preference | 22 (66.7%) | 0 (0.0%) | 11 (33.3%) | 0 (0.0%) |
| expanded_preference | 22 (66.7%) | 0 (0.0%) | 11 (33.3%) | 0 (0.0%) |

Order disagreements remain unresolved; they are not counted as ties.

### Eligibility sensitivity

The original denominator remains visible to show the effect of addressee adjudication. Excluding another participant's question does not change any generated answer or grade.

| Scope | N | Detections | Initial outputs | Our initial | Our expanded | Sensei | Initial win / tie / loss / disagreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Primary candidate scope | 33 | 22 | 22 | 3.18 | 3.22 | 2.37 | 22 / 0 / 11 / 0 |
| Original response-seeking labels | 35 | 22 | 22 | 3.06 | 3.09 | 2.37 | 23 / 0 / 12 / 0 |

## Coverage and timing

Confirmed/visible detections: 22/33. Initial answers: 22/33; deep answers: 22/33.
Coverage uses strict exported-turn card attribution, not semantic-question recall. Nearby turns can continue or merge one question (potential cases include 30/31 and 35/36), so a missing distinct card does not by itself establish an omitted semantic question. Answers and grades are never reused across those source turns.
Cards on turns labelled non-response-seeking: 2/64. These are provisional false-positive candidates, not confirmed errors.

| Stage, seconds from injection | p50 | p95 | N |
| --- | ---: | ---: | ---: |
| detected_s | 0.50 | 1.44 | 22 |
| fast_first_s | 1.11 | 2.70 | 22 |
| deep_first_s | 3.28 | 11.25 | 22 |
| fast_done_s | 1.70 | 3.22 | 22 |
| deep_done_s | 5.05 | 11.92 | 22 |

Initial answers after a predeclared 2-second injection deadline: 2. Missed answers are counted separately, never assigned a latency of zero.
Answers first appearing after the next response-seeking turn: 0. This uses recorded spacing, without assuming that timestamps mark question ends.

## Ten largest initial-answer deficits

Ranked by equal-weight score gap, not selected anecdotes. Categories below are diagnostic hypotheses unless directly established by events. Retrieval versus missing memory requires checking the private profile/pack evidence; scores alone cannot distinguish those causes.

| Turn | Paraphrased intent | Score deficit | Classification | Judge explanation |
| --- | --- | ---: | --- | --- |
| 0 | Exchange greetings and ask how the other person is doing. | 3.67 | not detected (observed) | The opening social exchange received neither detection nor an answer. |
| 95 | Do you have any questions for the HR team? | 1.83 | not detected (observed) | The closing invitation to ask HR questions produced no detection or response. |
| 52 | What tools and technologies do you typically use in your work? | 1.75 | not detected (observed) | The fragmented request about development tools produced no card, despite relevant technologies being available in memory. |
| 53 | Do you usually rely on templates when building things? | 1.67 | not detected (observed) | The interrupted templating question was not detected; reusable dashboard configurations and pipeline patterns were already available. |
| 39 | The role involves handling patient data. What do you know about the applicable compliance requirements? | 1.58 | not detected (observed) | The disfluent patient-information compliance prompt received no detection or answer. |
| 68 | Have you used Epic itself, or only worked with data exported from it? | 1.58 | not detected (observed) | The direct vendor-experience follow-up produced no response. The resume establishes EMR integration, not Epic-specific experience. |
| 86 | Describe your experience writing effective queries for established SQL databases such as MySQL, and designing schemas or indexes. | 1.42 | not detected (observed) | The multipart database-experience question was not detected, although SQL integration and SQL Server experience were available. |
| 18 | When someone on the product team asks you to create something, what happens in that process? | 1.33 | not detected (observed) | The unfinished product-request process prompt had no detection or output at this turn. |
| 35 | Engineers may need to negotiate requirements with product and other teams, represent engineering’s perspective, and raise constraints. How would you handle that? | 1.17 | prompt/shape (hypothesis) | No standalone answer appears, but the next card explicitly merges this turn and addresses negotiation. Combining distinct intents may have hidden coverage rather than indicating a complete detector miss. |
| 30 | When planning and building a product or feature, how do you ensure the design supports measuring its success after launch? | 0.92 | not detected (observed) | The incomplete planning prompt received no local detection or response; its later continuation was answered, so this is not evidence that the entire topic was missed. |

## Answer-quality deficits where an answer appeared

This separates weak answers from missed detection. Rows are ranked on the initial score gap on matched questions; zero or negative gaps are omitted.

| Turn | Paraphrased intent | Initial deficit | Expanded deficit | Judge explanation |
| --- | --- | ---: | ---: | --- |

No delivered answer has a positive aggregate initial-score deficit versus Sensei in this run. This does not imply that every delivered answer is satisfactory.

### Absolute quality risks in delivered answers

Relative wins can still have weak scores or unsupported personal claims. These rows include delivered answers below 4/5 overall or with a nonzero claim finding in either order. They are ranked by our initial score, not by a Sensei loss. Claim counts show the two judging orders separately.

| Turn | Paraphrased intent | Our initial / expanded | Initial claims | Expanded claims |
| --- | --- | ---: | --- | --- |
| 48 | Do you have much experience working with AWS? | 2.42 / 2.83 | [0, 0] | [0, 0] |
| 36 | How do you manage your time while working remotely? | 3.25 / 3.33 | [1, 1] | [1, 1] |
| 33 | Are you comfortable working on a small team at a small company? | 3.42 / 3.42 | [1, 1] | [1, 1] |
| 92 | Describe a time you worked with cross-functional teams to reach a shared goal. How did you make it happen? | 3.67 / 3.75 | [0, 0] | [0, 0] |
| 24 | What experience do you have maintaining a product over the long term after requirements, development, and deployment? | 3.92 / 4.08 | [1, 1] | [0, 0] |
| 26 | Asks about the candidate’s experience with small teams that build new products and maintain and iterate on deployed ones. | 4.00 / 4.00 | [1, 1] | [1, 1] |
| 27 | Can you explain in more detail how you built healthcare data workflows that integrated with hospital EMR systems, including how you accessed EMR data and used it in those workflows? | 4.17 / 4.00 | [0, 0] | [2, 3] |
| 37 | Do you have any concerns about working too much? | 4.25 / 4.00 | [1, 1] | [1, 1] |
| 70 | Are you familiar with the HL7 protocol and its data format, even though you do not currently use FHIR? | 4.25 / 4.42 | [1, 1] | [1, 1] |

## Strengths to preserve

- Turn 20: What do you need from a workplace to do your best work? — Identifies practical inputs for effective delivery, including user decisions, data meaning, access constraints, and acceptance checks, without demanding complete specifications.
- Turn 21: What level of guidance works best for you, from detailed requirements and polished mockups to broad goals or rough sketches? — Balances clarity with flexibility: rough designs and broad goals can become a small initial implementation through explicit checks and feedback.
- Turn 22: What would help you feel comfortable and effective in the role? — Keeps the response focused on actionable product guidance, with attention to missing information, permissions, and documentation proportional to risk.
- Turn 24: What experience do you have maintaining a product over the long term after requirements, development, and deployment? — Provides concise maintenance guidance linked to documented modular development, and the expanded answer distinguishes proposed monitoring from established experience.
- Turn 26: Asks about the candidate’s experience with small teams that build new products and maintain and iterate on deployed ones. — Uses a concrete, resume-supported reliability contribution involving player health reporting and offline playback, rather than relying only on maintenance generalities.
- Turn 27: Can you explain in more detail how you built healthcare data workflows that integrated with hospital EMR systems, including how you accessed EMR data and used it in those workflows? — The initial answer accurately connects documented API and SQL integration to clinical processing and review decisions without inventing EMR vendors or authentication details.
- Turn 31: How do you design for assessing whether a deployed solution is successful? — Connects advance measurement planning to baselines, task outcomes, operational signals, and later evaluation, presenting these as an approach rather than fabricated history.
- Turn 32: Do you add instrumentation or usage signals to determine whether a solution is working and being adopted? — Correctly treats usage traces as instrumentation, ties them to completed workflows and privacy controls, and supplies a documented activity-tracking example.
- Turn 33: Are you comfortable working on a small team at a small company? — Offers concise coordination and review practices without adopting the competitor's unsupported team composition, leadership, and delivery anecdotes.
- Turn 36: How do you manage your time while working remotely? — Despite its mixed focus, the answer provides usable prioritization and progress-sharing practices without inventing named productivity tools or detailed personal routines.

## Improvement plan — approval required before implementation

The priorities are provisional until calibration. Expected gain is a testable hypothesis, not a measured improvement. Effort estimates are relative.

The user waived human scoring. The separate AI review and addressee audit support consistency checks, while priorities remain provisional without human calibration. Opening pleasantries require an addressee/usefulness check before adding triggers. The ranked experiments below use an explicit planning assumption: recovering 25% of each observed deficit. The estimated gain is on the overall 1-5 initial score, divided by relative effort (small=1, medium=3, large=8) for ordering. These estimates are not measured effects, overlap across experiments, and must not be summed. For absolute-quality follow-ups that are not relative losses, the same 25% assumption applies to headroom between our initial score and 5; those entries are explicitly labelled and do not claim a measured Sensei deficit.

| Turn | Proposed experiment | Loss reason | Assumed score gain / effort |
| --- | --- | --- | --- |
| 0 | Investigate addressee and cue usefulness before any trigger implementation. Test a lightweight greeting detector that produces one brief conversational response. | not detected | +0.028 / small |
| 95 | Add closing-invitation detection tests and offer two concise questions about role expectations and hiring next steps. | not detected | +0.014 / small |
| 68 | Test contrastive follow-up detection and an evidence-boundary response that avoids asserting either Epic usage or Epic-derived data work. | not detected | +0.012 / small |
| 33 | Test an evidence-boundary format that presents documented full-stack breadth, avoids unsupported maintenance claims, and treats small-team practices as an approach. | prompt/shape | +0.012 / small (absolute quality; headroom to 5) |
| 92 | Test a behavioral format that states the documented project contribution and outcome while explicitly leaving undocumented collaboration details unclaimed. | too generic | +0.010 / small (absolute quality; headroom to 5) |
| 24 | Compare a version anchored in documented device diagnostics or pipeline maintainability, explicitly separating those contributions from proposed long-term maintenance practices. | too generic | +0.008 / small (absolute quality; headroom to 5) |
| 48 | Compare merged and isolated generation for this turn, requiring an experience-boundary statement before any proposed AWS practices. | prompt/shape | +0.020 / medium (absolute quality; headroom to 5) |
| 52 | Replay disfluent tooling questions and test intent recognition before grammatical completion, using only resume-supported technologies. | not detected | +0.013 / medium |
| 36 | Test latest-intent prioritization with separate sections for negotiation and remote work, covering prioritization, focus time, and blocker communication. | prompt/shape | +0.013 / medium (absolute quality; headroom to 5) |
| 53 | Test incomplete either-or questions and generate a brief answer grounded in the McKinsey configuration work, without adding deployment tools. | not detected | +0.013 / medium |
| 39 | Evaluate compliance-intent detection on noisy transcripts, separating general regulatory knowledge from documented access-control and auditing work. | not detected | +0.012 / medium |
| 86 | Replay long multipart questions and answer each part using documented database work, distinguishing proposed indexing techniques from past experience. | not detected | +0.011 / medium |
| 18 | Test bounded buffering of interrupted questions, emitting a clarification when the intended process question remains incomplete. | not detected | +0.010 / medium |
| 35 | Replay turns 35–36 with separate intent tracking and verify that each receives a visibly attributable response. | prompt/shape | +0.009 / medium |
| 30 | Test continuation-aware buffering and measure whether fragments are explicitly linked to the eventual answered question. | not detected | +0.007 / medium |

Every proposed change must compare per-criterion and per-type scores, agreement, unsupported claims, coverage and win regressions on the same evidence. No changes from this plan have been made.

## Calibration and reproducibility

The private human-review.html/JSON contains 15 seeded random questions from the original actionable-turn labels, with A/B answers and score fields. The user requested AI scoring instead. gpt-6.2-astra independently graded the same initial-answer sample once, with no primary grades or product labels. The table measures AI-to-AI agreement, not human calibration. Both reviewers share a provider family, so correlated biases remain possible. The original human trust threshold was 15 complete, preference agreement >=80%, and each criterion's mean absolute error <=0.75. Meeting that numerical threshold with AI scores does not satisfy the human trust gate.

Run the command in evals/README.md with the external export and private artifacts. Resume/pack construction is cached for comparable before/after runs. The committed fixture contains paraphrased questions only; original wording and Sensei answers are required locally to reproduce the original benchmark. A paraphrase-only regression run is a distinct experiment, not an exact reproduction.

Calibration status: **ai_agreement_only**. Completed 15/15. Preference agreement: 100.0%.

| Calibration criterion | Mean absolute error |
| --- | ---: |
| relevance | 0.383 |
| grounding | 0.317 |
| specificity | 0.333 |
| structure | 0.167 |
| speakability | 0.100 |
| first_two_lines | 0.400 |

Review covers initial answers only; expanded grades remain model-only. Human reviews completed: 0/15.

## Spend

Usage-accounted calls: $4.769219. Conservative unresolved-call/embedding reservations: $4.314799. **Combined conservative ledger: $9.084018.** The user lifted the original $3 cap. The ledger includes the earlier aborted replay and failed preparation attempts. Unknown usage is reserved conservatively. No new billed infrastructure. Usage-accounted amounts apply configured token prices; cache-write premiums and billing adjustments are not exposed by the live adapter. The combined ledger is an estimate with unresolved allowances, not an invoice or a guaranteed billing upper bound.

Prices checked against [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). Setup, embeddings, detector calls, prewarming, fast/deep generation, annotations and both judge orders, independent AI review and the addressee audit are included. Independent reviewer rates use the [GPT-6.2 Astra model page](https://developers.openai.com/api/docs/models/gpt-6.2-astra).

| Model | Calls | Usage-accounted USD | Unresolved allowance USD |
| --- | ---: | ---: | ---: |
| gpt-6-astra | 72 | 4.039980 | 0.000000 |
| gpt-6-luna | 269 | 0.036193 | 0.276268 |
| gpt-6.1-sol | 32 | 0.108414 | 4.019302 |
| gpt-6.2-astra | 16 | 0.584632 | 0.000000 |
| text-embedding-3-large | 123 | 0.000000 | 0.019229 |

## Unsupported personal claims

Counts are judge findings against the supplied resume/JD, not independently verified fabrications. A claim absent from these sources may still be true. The two order counts are shown separately so disagreement is visible; they are not added together.

| Side and answer | Order 1 total | Order 2 total |
| --- | ---: | ---: |
| ours initial | 6 | 6 |
| ours expanded | 7 | 8 |
| sensei initial | 154 | 163 |
| sensei expanded | 154 | 163 |

## Per-question audit

Scores average six criteria across both orders. Claim counts are initial-order pairs. Stage timings, answer text and source references remain in private answers.json.

| Turn | Expected / detected type | Our initial / expanded | Sensei | Preference | Claims ours / Sensei | Fast first / deep first (s) | Large score disagreement |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| 0 | small_talk / not detected | 1.00 / 1.00 | 4.67 | sensei | [0, 0] / [0, 0] | missing / missing | False |
| 18 | technical / not detected | 1.00 / 1.00 | 2.33 | sensei | [0, 0] / [1, 1] | missing / missing | False |
| 19 | technical / not detected | 1.00 / 1.00 | 1.75 | sensei | [0, 0] / [8, 9] | missing / missing | False |
| 20 | behavioral / other | 4.83 / 5.00 | 2.25 | ours | [0, 0] / [0, 0] | 0.83 / 11.25 | False |
| 21 | behavioral / behavioral | 4.83 / 4.92 | 2.83 | ours | [0, 0] / [0, 0] | 1.52 / 8.22 | False |
| 22 | behavioral / behavioral | 4.83 / 5.00 | 2.25 | ours | [0, 0] / [0, 0] | 1.67 / 11.14 | False |
| 24 | behavioral / behavioral | 3.92 / 4.08 | 1.83 | ours | [1, 1] / [4, 5] | 1.11 / 2.72 | False |
| 26 | behavioral / behavioral | 4.00 / 4.00 | 2.08 | ours | [1, 1] / [7, 7] | 0.88 / 3.28 | False |
| 27 | technical / technical | 4.17 / 4.00 | 2.08 | ours | [0, 0] / [9, 10] | 1.22 / 6.95 | False |
| 30 | behavioral / not detected | 1.00 / 1.00 | 1.92 | sensei | [0, 0] / [7, 7] | missing / missing | False |
| 31 | behavioral / system_design | 4.75 / 4.33 | 2.08 | ours | [0, 0] / [8, 7] | 2.92 / 8.05 | False |
| 32 | technical / technical | 4.67 / 4.92 | 1.75 | ours | [0, 0] / [8, 9] | 0.88 / 5.89 | False |
| 33 | behavioral / behavioral | 3.42 / 3.42 | 1.83 | ours | [1, 1] / [8, 8] | 0.98 / 2.84 | False |
| 35 | behavioral / not detected | 1.00 / 1.00 | 2.17 | sensei | [0, 0] / [3, 3] | missing / missing | False |
| 36 | behavioral / behavioral | 3.25 / 3.33 | 1.83 | ours | [1, 1] / [11, 12] | 0.98 / 3.09 | False |
| 37 | behavioral / respond | 4.25 / 4.00 | 1.58 | ours | [1, 1] / [8, 10] | 1.11 / 2.91 | False |
| 39 | technical / not detected | 1.00 / 1.00 | 2.58 | sensei | [0, 0] / [1, 1] | missing / missing | False |
| 40 | behavioral / technical | 4.75 / 4.92 | 1.75 | ours | [0, 0] / [8, 8] | 0.83 / 7.56 | False |
| 41 | technical / technical | 4.33 / 4.83 | 2.83 | ours | [0, 0] / [0, 0] | 2.70 / 11.89 | False |
| 48 | technical / technical | 2.42 / 2.83 | 2.42 | ours | [0, 0] / [6, 5] | 0.91 / 2.84 | False |
| 52 | technical / not detected | 1.00 / 1.00 | 2.75 | sensei | [0, 0] / [2, 3] | missing / missing | False |
| 53 | technical / not detected | 1.00 / 1.00 | 2.67 | sensei | [0, 0] / [3, 3] | missing / missing | False |
| 54 | technical / technical | 5.00 / 4.92 | 2.83 | ours | [0, 0] / [3, 3] | 1.19 / 6.03 | False |
| 55 | small_talk / other | 4.83 / 4.75 | 2.83 | ours | [0, 0] / [0, 0] | 1.23 / 3.23 | False |
| 66 | technical / technical | 4.58 / 4.42 | 2.50 | ours | [0, 0] / [4, 4] | 0.89 / 3.14 | False |
| 68 | technical / not detected | 1.00 / 1.00 | 2.58 | sensei | [0, 0] / [5, 5] | missing / missing | False |
| 70 | technical / technical | 4.25 / 4.42 | 2.17 | ours | [1, 1] / [6, 6] | 1.88 / 3.25 | False |
| 86 | technical / not detected | 1.00 / 1.00 | 2.42 | sensei | [0, 0] / [6, 6] | missing / missing | False |
| 89 | behavioral / other | 4.33 / 4.67 | 2.17 | ours | [0, 0] / [11, 12] | 1.42 / 11.00 | False |
| 91 | behavioral / other | 4.42 / 4.17 | 2.33 | ours | [0, 0] / [1, 1] | 0.56 / 10.78 | False |
| 92 | behavioral / behavioral | 3.67 / 3.75 | 2.67 | ours | [0, 0] / [10, 11] | 1.22 / 2.89 | False |
| 93 | behavioral / behavioral | 4.50 / 4.50 | 2.50 | ours | [0, 0] / [6, 7] | 0.94 / 2.83 | False |
| 95 | respond / not detected | 1.00 / 1.00 | 2.83 | sensei | [0, 0] / [0, 0] | missing / missing | False |
