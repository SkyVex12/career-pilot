# Answer coverage change — 2026-11-19

The completed final-v3 run at source `79c3181` produced answers on **32/33 primary turns, up from 22/33**, and 44 surviving cards across all 99 interviewer turns. Our coverage-inclusive initial score rose from **3.18 to 4.25**, versus Sensei **2.32** in the final judging run. On the same 22 previously answered questions, initial quality changed only **+0.02**; the main improvement is coverage. Semantic repetition and precision limitations remain below. These are provisional AI grades, without human calibration.

The change makes recognizable candidate-directed questions and brief exchanges eligible for an answer, including greetings, farewells, personal good wishes, audio/logistics checks, short factual or yes/no questions, confirmation checks and closing invitations. Both detector APIs use this policy. A recognizable ask can survive trailing filler or an unfinished extra clause. Pure exposition, internal rhetorical questions, acknowledgements without a reply sought and explicit other addressees remain excluded.

Brief exchanges receive one or two concise cues, with no padding, invented personal update, guessed personal yes/no answer or assertion that an action has already happened. A settled, lexically identifiable ask can recover from a detector wait/error in a one-to-one call after the continuation grace period. Explicit `none`/duplicate decisions remain authoritative; group-call recovery does not bypass addressee checks.

## Repetition and conversation behavior

Automatic duplicate suppression compares the normalized question with the latest usable automatic card within the configured continuation window, capped at ten seconds, using both wall and capture timing. An active or successfully completed card can suppress an immediate repeat. Failed, empty or withdrawn cards remain retryable after generation ends. Substantive intervening speech, a changed question, an expired window or a deliberate manual force permits another answer. Recognizable new asks and brief replies retain their own boundaries. Continuation handling retains context, but does not guarantee that every continuing question shares one card; the output audit below makes that distinction explicit.

Generation receives at most three earlier completed, confirmed, visible fast cues in a separate `<earlier_suggestions>` block. These are explicitly untrusted UI suggestions, not candidate speech or source facts, and do not enter spoken-answer memory. New follow-ups request the new detail; explicit repeat requests remain allowed. Identical substantial cached/prepared cues fall through to fresh generation instead of suppressing the new question. This is bounded repetition control, not a semantic guarantee that answers never overlap.

## Comparable completed evidence

All 99 interviewer turns were replayed at 1.0x. The **primary 33-turn denominator and original labels remain unchanged**, including ambiguous cases; explicit other addressees 3 and 8 remain excluded. The source resume/JD, pack, historical Sensei suggestions and blind six-criterion judging protocol are held fixed. Initial means include missing answers scored as 1; expanded means use deep output or fast fallback. Scores and deltas are rounded to two decimals; deltas are calculated before rounding. The completed intermediate run predates the closing-courtesy and continuation-boundary fixes and remains separately labelled development evidence.

| Fixed primary scope | Baseline | Completed intermediate (`b78c083`) | Final-v3 (`79c3181`) |
| --- | ---: | ---: | ---: |
| Detected turns | 22/33 | 32/33 | 32/33 |
| Nonempty fast outputs | 22/33 | 31/33 | 32/33 |
| Nonempty deep outputs | 22/33 | 27/33 | 28/33 |
| Our initial score | 3.18 | 4.20 | 4.25 |
| Our expanded score | 3.22 | 4.27 | 4.30 |
| Sensei score in that judging run | 2.37 | 2.34 | 2.32 |
| Initial win / tie / loss / order disagreement | 22 / 0 / 11 / 0 | 30 / 0 / 2 / 1 | 31 / 0 / 2 / 0 |
| Expanded win / tie / loss / order disagreement | 22 / 0 / 11 / 0 | 30 / 0 / 2 / 1 | 31 / 0 / 2 / 0 |
| Our unsupported initial claims, two orders | 6 / 6 | 8 / 8 | 7 / 6 |
| Our unsupported expanded claims, two orders | 7 / 8 | 8 / 8 | 10 / 10 |

The final initial and expanded win rates are both 93.9%. Sensei's final unsupported-claim totals are 155 / 159 in the two orders. Claim counts are not added across orders. The final losses were an undetected implicit stakeholder-negotiation request (35) and a greeting that reciprocated without answering the wellbeing check (0). Broader coverage does not mean every delivered answer is strong: platform-experience, small-team and collaboration answers remain weak, while some expansions introduce unsupported personal detail. Relative wins do not erase these absolute risks.

The overall increase primarily reflects coverage. The paired comparison retains the **same 22 baseline-delivered questions in both runs**, including any subsequently missing output; newly recovered questions do not enter this cohort. Separate per-run conditional means are not a paired comparison.

| Fixed baseline-delivered cohort (n=22) | Baseline | Final-v3 | Change |
| --- | ---: | ---: | ---: |
| Our initial score | 4.27 | 4.29 | +0.02 |
| Our expanded score | 4.33 | 4.30 | -0.02 |
| Our initial unsupported claims, two orders | 6 / 6 | 4 / 3 | -2 / -3 |
| Our expanded unsupported claims, two orders | 7 / 8 | 8 / 9 | +1 / +1 |
| Initial wins against Sensei | 22 | 22 | 0 |
| Expanded wins against Sensei | 22 | 22 | 0 |

In this paired cohort, initial relevance improved +0.23 and first-two-lines usefulness +0.14, but structure declined -0.14 and speakability -0.27. Expanded grounding declined -0.16. The intermediate paired initial/expanded means were 4.16 / 4.32, or -0.11 / -0.01 against baseline. The final results do not establish a broad quality improvement on already answered questions. Sensei's unchanged suggestions were regraded independently, so its small score movement is judging variation, not changed output.

Final-v3 latency is measured from text injection, not verified question end:

| Stage | p50 seconds | p95 seconds | Outputs |
| --- | ---: | ---: | ---: |
| Detection | 0.53 | 1.92 | 32 |
| First fast output | 1.22 | 1.92 | 32 |
| First deep output | 7.61 | 11.00 | 28 |
| Fast completion | 1.84 | 2.34 | 32 |
| Deep completion | 7.72 | 11.08 | 28 |

One initial output missed the predeclared two-second injection deadline; no initial output first appeared after the next response-seeking turn. Missing answers are counted separately. Deep responses remain materially slower, and audio/STT plus UI rendering are not included.

## Supplemental reply policy and remaining limits

An independent interviewer-only `gpt-6-astra` audit classified 99 turns as 30 reply, 56 none, 9 continuation and 4 unclear. It selected seven additional reply opportunities outside the primary scope **before inspecting either assistant's answers**: 11, 29, 34, 67, 84, 97 and 98. These labels do not revise the primary denominator. In particular, continuations do not automatically require additional cards.

The frozen policy labels were applied to final-v3 outputs without relabeling:

| Independent policy category | Labelled turns | Turns with a surviving output |
| --- | ---: | ---: |
| Reply | 30 | 28 |
| None | 56 | 6 |
| Continuation | 9 | 9 |
| Unclear | 4 | 1 |

All 44 cards survived and were attributed to source turns. The six outputs on audit-`none` turns (17, 18, 30, 44, 52, 88) are possible unwanted replies requiring review. Each of the nine continuation-labelled turns produced a separate card, rather than merely updating the prior card. This is card proliferation, but **not evidence that all nine are duplicate answers**: the semantic review did not flag them as clear unnecessary repeats, and several added useful context to the continuing ask. Do not substitute this policy scope for the frozen primary 33-turn comparison.

The separate, exploratory seven-turn comparison includes missing outputs and uses the original rubric with randomized/swapped A/B order:

| Same seven policy-selected turns | Intermediate | Final-v3 |
| --- | ---: | ---: |
| Nonempty fast outputs | 3/7 | 5/7 |
| Our initial / expanded score | 2.50 / 2.51 | 3.67 / 3.44 |
| Sensei initial / expanded score | 3.08 / 3.08 | 3.02 / 3.02 |
| Initial and expanded win / tie / loss / disagreement | 3 / 0 / 4 / 0 | 4 / 0 / 2 / 1 |
| Our initial unsupported claims, two orders | 0 / 0 | 1 / 1 |
| Our expanded unsupported claims, two orders | 0 / 0 | 2 / 2 |
| Sensei unsupported claims, two orders | 14 / 14 | 15 / 15 |

Final-v3 fast outputs appeared for 11, 29, 67, 97 and 98; deep outputs for 11, 67 and 97. Closing turns 97/98 therefore recovered in the full replay. The remaining misses, 34 and 84, are ambiguous: a brief check mixed with exposition, and a personal courtesy mixed with discussion context. Earlier annotations treated them as exposition; the broader audit inferred a reply opportunity. Preserve and disclose this disagreement rather than force every fragment to produce an answer. The fresh supplemental judging cost $0.439340 and reused the existing independent policy selection.

The actual configured detector also ran on 13 entirely fictional contrasts: **12/13 decision matches and 7/7 expected positive types**, with no provider errors. It incorrectly answered a farewell explicitly addressed to another participant. This is a precision regression target; the policy does not guarantee perfect addressee discrimination, recall or precision.

The final-v3 lexical audit found **zero exact repeated substantial answers and zero duplicate-question card pairs within ten seconds**, with one shared-prefix pair between different deep-answer cards. It excludes answers under 20 words from answer-text repetition checks and tests normalized exact/prefix overlap. **A lexical audit is not a semantic non-repetition guarantee.**

A separate exploratory semantic review agreed across two orders on one baseline repetition flag among 24 visible fast cards, and two final-v3 flags among 44. The final flags were distinct follow-ups 31→32 and 67→68: the later answer reused earlier advice instead of sufficiently answering the new detail. These are different delivered-card cohorts, so the counts are not a fair before/after quality score. They establish that semantic repetition remains unresolved, not that the feature guarantees its elimination. The review cost $0.669560, did not change primary grades or policy labels, and has no human validation.

Reconstructing context selection from the recorded events places the relevant earlier completed cue within the configured limit for both flagged follow-ups; full model request bodies were not persisted. The later questions retained their wording. Both were ultimately classified technical, although speculative fast generation began before classification. All fast/deep outputs were fresh model generations, and the same evidence node was available in the fixed interview digest/pack; company-memory retrieval was inactive. Generation choice or source salience is therefore a plausible cause; retrieval omission is not established. These repetition flags concern fast cards. The main judge rated relevance higher in both expansions, so the flags do not establish that the full answers simply repeated.

CI for the frozen production source `79c3181` succeeded with **1,397 tests**. This validates the automated checks; the fictional addressee error and semantic repetition findings remain limitations.

There is **no human validation**. The baseline, intermediate and final main runs each received independent AI review of 15 samples with 100% preference agreement. The final reviewer differs from the primary judge, but both share a provider family. This measures consistency, not human validity; expanded grades have no separate calibration review. Claim findings mean unsupported by supplied sources, not independently established falsehoods. Source-turn card attribution is not semantic-question recall; candidate speech, audio/STT and UI render delay are absent, and export timestamp semantics are unknown. Historical Sensei configuration/context was not reconstructed. The 99 exported suggestions also make its observed coverage 100% by construction.

## Ranked remaining work

These are proposed follow-ups, not implemented changes or measured future gains. Preserve the fixed primary and paired cohorts, and evaluate precision and claim counts alongside coverage.

| Priority | Next experiment | Evidence and intended benefit | Relative effort |
| --- | --- | --- | --- |
| 1 | Require the later answer to address a follow-up's new requested detail, using prior cues only as untrusted context; keep the new question's card. | Semantic repeats 31→32 and 67→68. Improve usefulness without suppressing distinct asks or treating generated advice as resume evidence. | Medium |
| 2 | Recover implicit stakeholder requests with matched negative exposition controls. | Missing primary turn 35. Test whether coverage improves without turning ordinary interviewer commentary into answers. | Medium |
| 3 | Tighten addressee and `none` precision, retaining uncertain cases for review. | Fictional other-addressee farewell and six audit-`none` outputs. Reduce unwanted cards while preserving recovered closings; do not force ambiguous 34/84 to answer. | Medium |
| 4 | Preserve brief reply length and improve shape/grounding on the paired cohort, especially deep expansions. | Initial structure -0.14, speakability -0.27 and expanded grounding -0.16. Keep one/two-cue brief exchanges and require support for personal claims. | Small to medium |

## Spend

Amounts are private-ledger estimates in USD. Reservations retain unknown usage and embedding allowances; they are not confirmed charges, an invoice or a guaranteed billing upper bound. The user lifted the original $3 cap. No new billed infrastructure was introduced. Stopped runs remain in costs but are excluded from outcome comparisons.

| Run or audit | Usage-accounted | Unresolved allowance | Role |
| --- | ---: | ---: | --- |
| Baseline, 512 ledger entries | 4.769219 | 4.314799 | Completed comparison; includes earlier preparation/replay attempts |
| Early coverage attempt, 85 entries | 0.035666 | 1.242534 | Stopped at 23/99 for capture-anchor bug; excluded from outcomes |
| Completed intermediate, 470 entries | 4.055473 | 7.026377 | Replay, blind judging and AI review above |
| Final-v2 attempt, 9 entries | 0.000965 | 0.011603 | Stopped at 2/99 after continuation-boundary review; excluded from outcomes |
| Independent policy audit and seven-turn judging, 16 entries | 0.701920 | 0.000000 | 0.311150 classification + 0.390770 judging |
| Selected-turn detector preflight, 16 entries | 0.001933 | 0.000990 | Detector-only development evidence |
| Fictional 13-case detector check, 15 entries | 0.001408 | 0.002010 | 12/13 decisions; limitation above |
| Final-v3 replay/main judging, 447 entries | 4.185551 | 6.275322 | Completed final primary comparison; combined estimate 10.460873 |
| Final-v3 seven-turn supplemental judging, 14 entries | 0.439340 | 0.000000 | Same independently selected policy cohort |
| Baseline/final-v3 semantic repetition review, 2 entries | 0.669560 | 0.000000 | Exploratory review; different visible-card cohorts |
| **All runs and audits** | **14.861035** | **18.873635** | **Combined estimate 33.734670** |
| **Feature work, excluding baseline** | **10.091816** | **14.558836** | **Combined estimate 24.650652** |

An additional 30 preflight connection attempts failed before submission and carry zero ledger charge. Intermediate and final source versions differ; their outcomes remain separate. Costs above include every listed run and audit once, including aborted attempts and reused preparation in the baseline ledger.

Only paraphrased fixtures and aggregate results belong in the repository. Raw interview text, answers, resume/JD evidence, private identifiers and credentials remain outside it. See the [final benchmark](2026-11-19-answer-coverage-benchmark.md) for per-question grades, source-claim risks and judging details, the [paired comparison](2026-11-19-answer-coverage-benchmark.comparison.md), the [original benchmark](2026-11-19-benchmark-sensei.md) and [eval instructions](../../evals/README.md).
