# Speed round 1, baseline: where the first second and a half goes

Target: the short answer's first word on screen **under 1.0 s at p50 and 1.5 s at p95** after
the question ends. Earlier work on latency is in [2026-10-07](2026-10-07-live-test.md),
[2026-10-14](2026-10-14-latency-fixes.md), [2026-10-30](2026-10-30-standup.md) and
[2026-11-11](2026-11-11-multi-session.md). What shipped from it: the held fast pass, one
detector call per turn, no wait for the prepared match, the cache prewarm, per-model prepared
thresholds, Claude keep-alive and warm-up, the shorter detector output, hedges on Priority
for all three passes (detector at 1.2 s), the early decision in standups, and showing a plain
interview question before the detector decides.

## Summary

On the benchmark (main at `7f16bb4`, gpt-6-luna for the detector and the fast pass,
gpt-6.1-sol for the detailed answer, Deepgram Nova-3, gateway on this desktop), the first
word comes **2.20 s at p50 and 2.85 s at p95** after the true end of speech. Not one of 35
questions came under 1.5 s.

- **The detector sets the time on 26 of 35 questions.** The fast pass already had its first
  token, or a prepared answer was ready, when the decision came.
- **Everything starts on the STT final, 0.70 s after speech ends.** On every question the
  detector and the held pass restarted when the final arrived, because the final changed the
  words. In October's saved runs the final matched the last interim's words only 36 times out
  of 141, and 37 utterances had no interim at all.
- **The model calls themselves take about 1.1 s to a first token**, and this desk is 270 ms
  from every provider. So even with no detector, the fast pass's first token lands at 1.9 s.

No single change reaches 1.0 s from this setup. The ranked list (section 4) gets the p50 to
about 1.5 to 1.7 s with no loss of detection. Going further needs the fast pass to start
before the final, a faster model, or a gateway near the providers.

## 1. How it was measured

`copilot-bench run --label speed-baseline --runs 3`: the scripted 4 min 15 s mock interview
(evals/README.md, "Latency benchmark"), replayed through the real gateway and providers, with
a cold prompt cache at each run start. Results are in `evals/bench/results/speed-baseline/`.
Every stage is measured from the **true end of speech** in the audio, as a running total, over
the 35 automatic questions that should get an answer.

New in this branch, so the stages could be measured:

- `decided_ms` on every answer (`answers.latency_ms`): the speech-end frame to the detector's
  decision. `detector_ms` is only the call's own length. It says nothing about when the card
  could show, once the call starts before the speech end or restarts on the final.
- The benchmark reports the path as running totals from the true end of speech, the fast
  pass's prompt cache hit rate, and how often the held pass was ready before the decision.

Provider probe medians (five raw detector calls before each run): 2.17, 1.22 and 1.66 s. That
is a normal afternoon, not a slow spell.

## 2. Baseline per stage

From the true end of speech, ms, n = 35:

| Stage | p50 | p95 |
| --- | --- | --- |
| Speech-end frame reaches the gateway (client VAD hangover) | 228 | 242 |
| STT final arrives | 703 | 967 |
| Detector decision | 2,186 | 2,853 |
| Fast-pass first token (model answers, n = 23) | 1,886 | 2,353 |
| **First word at the client** | **2,204** | **2,853** |
| Detailed answer done, at the client | 7,405 | 10,367 |

Durations behind them: detector call 1,469 / 1,971; fast-pass request to first token 1,125 /
1,594; detailed answer's first word 4,414 / 7,563.

By path (first word, p50 / p95):

| Path | n | First word | What it waits for |
| --- | --- | --- | --- |
| Detector-gated, model answer | 15 | 2,355 / 2,855 | The detector; the token was ready first on 14 |
| Detector-gated, prepared answer | 12 | 2,204 / 2,477 | The detector; the points were ready |
| Shown before the detector (`first_show`) | 8 | 1,858 / 2,343 | The fast pass's first token |

Other numbers from the run:

| | Value |
| --- | --- |
| Precision / recall | 1.00 / 0.97 (one miss, `do_differently`, in run 1) |
| Shown, then withdrawn | 1 in 3 runs (the same miss: shown first, then the detector said no) |
| Fast pass prompt | 4,261 tokens, 90% read from the cache, no uncached pass after the prewarm |
| Held passes thrown away | 68% of fast starts, $0.002 a run |
| Prepared answers | 12 of 15 right, none wrong |
| Cost per live hour | $0.78 models, $0.95 with Deepgram |

## 3. Probes

**Model and tier**, the gateway's own detector and fast-pass prompts, 8 rounds interleaved,
warm connection and warm cache (first token and whole response, ms):

| Call | First token p50 / p95 | Whole p50 / p95 |
| --- | --- | --- |
| Detector, gpt-6-luna, default tier | 1,031 / 1,625 | 1,360 / 1,937 |
| Detector, gpt-6-luna, **Priority** | 906 / 1,203 | 1,140 / 1,531 |
| Fast pass, gpt-6-luna, default tier (3.9k tokens) | 1,047 / 1,391 | 1,922 / 2,422 |
| Fast pass, gpt-6-luna, **Priority** | 890 / 1,078 | 1,328 / 2,313 |
| Fast pass, rules only (2.3k tokens) | 1,015 / 1,734 | 1,938 / 2,594 |
| Fast pass, digest three times (5.3k tokens) | 1,031 / 2,218 | 1,859 / 3,312 |
| Detector on a new connection | 1,672 to 1,891 | |

- Priority takes about 0.15 to 0.2 s off p50 and 0.3 to 0.4 s off p95, on both calls.
- **Prompt length does not move the first token**: 2.3k, 3.9k and 5.3k cached tokens all
  start in about 1.0 s. The cache already hits 90%. Trimming the fast-pass prompt buys
  nothing.
- **A cold connection costs about 0.7 s.** Idle connections are kept 120 s and warmed at
  session start. A question after more than two quiet minutes (a long answer by the user)
  pays it again.
- **Claude Haiku 5.5 and Haiku 4.5 could not be measured.** The `ANTHROPIC_API_KEY` in
  `.env` returns 401 (invalid key).

**Network.** TCP connect from this desk is 270 to 300 ms to api.openai.com,
api.deepgram.com and api.anthropic.com alike. A new TLS session is 0.55 s or more.

**The STT final.** From October's saved runs (141 finals): the final matched the last interim's
words 36 times, added 1 to 3 words at the end 56 times, had other words 12 times, and had no
interim before it 37 times. The final came 0.42 s after the last interim (p50). So a decision
made on the interim is thrown away three times in four. This is why the early decision helped
on only 4 of 18 standup questions.

**A local question check, offline**, on the 119 labelled lines of the other side in
`evals/bench/*.toml` and `evals/sessions/*.toml` (35 interview questions and 34 interview
non-questions; 19 and 31 in standups):

| Check | Interview questions passed | Interview non-questions passed | Standup questions passed | Standup non-questions passed |
| --- | --- | --- | --- | --- |
| `likely_question` (gates the held pass) | 35 / 35 | 23 / 34 | 17 / 19 | 12 / 31 |
| `plain_question`, 8 words (shown before the detector) | 9 / 35 | 0 / 34 | 0 / 19 | 3 / 31 |
| A wider rule, 8 words (a trailing "What happened?", ask-verbs with no "?") | 14 / 35 | 0 / 34 | 1 / 19 | 3 / 31 |
| `shahrukhx01/bert-mini-finetune-question-detection` | 35 / 35 | **34 / 34** | 19 / 19 | 28 / 31 |

The BERT-mini model runs in 2.5 ms a line on this CPU. But it was trained to tell keyword
search queries ("bake bread recipe") from sentences, and it puts questions and statements in
the same class. It passes the goodbye and the team introduction as readily as a question, so
it cannot gate anything here (section 4, row 8).

## 4. Fixes, ranked by milliseconds saved against effort and risk

"Saved" is first word at p50 / p95 on this benchmark unless noted. Cost is per live hour at
the benchmark's rates (14 runs an hour; today $0.95).

| # | Fix | Saved | Cost / live hour | Risk to detection | Effort |
| --- | --- | --- | --- | --- | --- |
| 1 | **Priority tier on the first route** of the detector and the fast pass (today Priority is only on the hedge) | ~200 / ~350 ms on every card | +$0.10 (detector and fast calls 1.75x) | None: same model, same prompt | A setting per job |
| 2 | **Prepared answer shown before the detector**: a hit at a stricter similarity (0.70 and the margin) shows at once, and is withdrawn if the detector says no | ~0.8 s on prepared hits (12 of 35 here; 2 of 62 in the 10-29 calls) | None: no model call | A rare card shown, then withdrawn, on a non-question that resembles a prepared question. The calibration set decides the threshold | Small (`turn.py`, `answer.py`) |
| 3 | **Shown before the detector on more questions**: the wider local rule (14 of 35 instead of 9, no false pass on the labels) | ~0.4 s on about 5 more questions in 35 | None | Standups stay out. Interview false passes: 0 of 34 on the labels; a real call will withdraw one now and then | Small (`turn.py`) |
| 4 | **Keep connections warm through quiet spells**: a free request to each provider every 60 s while a session is live | ~0.7 s on a question after more than 2 minutes of quiet (not on this benchmark) | ~0 (a models list is unbilled) | None | Small |
| 5 | **Start the fast pass on the interim and keep it** when the final adds only 1 to 2 words at the end (today it restarts on the final every time) | ~450 ms where it applies (about 40% of finals) | Fewer restarts, about even | None on detection. Answer quality on a question missing its last word must be checked with the answer eval first | Medium (`turn.py`) |
| 6 | **Claude Haiku 5.5 for the detector and the fast pass** ($0.10 / $0.50, 512-token cache minimum) | Unknown until measured | About the same as gpt-6-luna | Must be re-run on the detector and session evals | Needs a valid Anthropic key; then a setting |
| 7 | **STT: Deepgram Flux** (`/v2/listen`, end of turn in about 260 ms with an early end-of-turn event) instead of the client VAD plus Finalize | Up to ~0.4 s on the final, if the claim holds | Different model and price, keyterms not confirmed | Turn ends decided by Deepgram, not by our VAD; all turn handling to re-test | Large: a new STT client and protocol work |
| 8 | **Local classifier ahead of the detector** | — | — | The BERT-mini model above cannot tell a statement from a question. A model that can, and knows the addressee, needs a labelled corpus we do not have (119 lines) | Large; not now |
| 9 | Trim the fast-pass prompt; improve the cache hit rate | ~0 (measured) | — | — | Not worth doing |

Not counted above: running the gateway near the providers. Stage 0's VM is in a US region, so
live calls already skip most of this desk's 270 ms round trips.

**What the set reaches.** With 1 to 4 on this benchmark: detector-gated cards about 2.0 s,
cards shown first about 1.6 s, prepared hits about 1.4 s, so a p50 near 1.7 s and a p95 near
2.4 s. Adding 5 moves cards shown first to about 1.2 s. The 1.0 s target needs the first token
at about 0.5 s after the speech end. That is half today's request-to-token time alone, and no
OpenAI model measured here gets there.

## 5. Spend

Three benchmark runs, $0.20 (models $0.17, Deepgram $0.03); the model probe $0.004; the
local checks and the BERT-mini check $0 (offline). About $0.21 of the $2 limit.
