# Sensei before/after comparison

Same source evidence, pack, labels, addressee eligibility and judge. Explicit other addressees are excluded; unclear cases are retained. Human calibration is not established for both runs. Any AI-only review measures judge consistency, not human validity.

Fixed primary scope: 33 turns. Replay settings, model routes and speed must also be checked against the recorded replay identities.

| Coverage in the fixed scope | Before | After |
| --- | ---: | ---: |
| Surviving visible-card turns | 22/33 | 32/33 |
| Nonempty initial-output turns | 22/33 | 32/33 |
| Nonempty deep-output turns | 22/33 | 28/33 |
| Recorded card IDs, including withdrawn | 22 | 32 |
| Turns with multiple recorded card IDs | 0 | 0 |

Coverage is exported-turn card attribution, not semantic-question recall. Nonempty outputs include streamed partial answers; completion and error events require separate audit. Multiple card IDs can include withdrawn speculation or replacement cards and do not alone prove repeated visible answers. Newly supported exchanges outside this fixed scope require a separate policy-aware audit.

## Fixed-scope quality, including missing answers

| Criterion | Initial before | Initial after | Change | Expanded change |
| --- | ---: | ---: | ---: | ---: |
| relevance | 2.97 | 4.11 | +1.14 | +1.12 |
| grounding | 3.42 | 4.62 | +1.20 | +1.03 |
| specificity | 2.67 | 3.59 | +0.92 | +1.02 |
| structure | 3.21 | 4.17 | +0.95 | +1.09 |
| speakability | 3.56 | 4.53 | +0.97 | +0.98 |
| first_two_lines | 3.26 | 4.47 | +1.21 | +1.23 |
| Overall | 3.18 | 4.25 | +1.07 | +1.08 |

## Baseline-delivered initial-answer cohort (n=22)

| Criterion | Initial before | Initial after | Change | Expanded change |
| --- | ---: | ---: | ---: | ---: |
| relevance | 3.95 | 4.18 | +0.23 | +0.02 |
| grounding | 4.64 | 4.70 | +0.07 | -0.16 |
| specificity | 3.50 | 3.57 | +0.07 | -0.07 |
| structure | 4.32 | 4.18 | -0.14 | +0.02 |
| speakability | 4.84 | 4.57 | -0.27 | -0.07 |
| first_two_lines | 4.39 | 4.52 | +0.14 | +0.11 |
| Overall | 4.27 | 4.29 | +0.02 | -0.02 |

This cohort keeps the same baseline-delivered questions in both columns. A newly missing answer remains scored as missing; newly recovered questions do not enter this cohort. Each run's separate conditional mean can cover different questions and is not a paired quality comparison.
