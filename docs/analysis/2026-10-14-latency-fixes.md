# Latency fixes, 2026-10-14: benchmark results and the remaining floor

Follow-up to [the 2026-10-07 live test](2026-10-07-live-test.md), which measured the first word
at 2.7 s p50 / 5.3 s p95 against the 0.9 s / 1.5 s target and ranked the causes. This branch
adds a repeatable benchmark, fixes the ranked issues in order with one commit and one test each,
and measures each fix on the benchmark. Everything runs locally (gateway on a Windows desktop,
OpenAI only: gpt-6-luna for the detector and the fast pass, gpt-6.1-sol for the deep pass,
text-embedding-3-large, Deepgram Nova-3).

## Summary

On the benchmark, from this desktop and with OpenAI only, the first word went from **5.0 s
p50 / 9.2 s p95** (baseline) to **2.3 s p50 / 3.2 to 4.3 s p95** after fixes 1 to 5 (2 runs
each). The largest steps were the held fast pass (fix 1), one detector call per turn (fix 2) and a
bug the benchmark found, utterances glued together when the detector is slower than the gap
between sentences (2b), which was most of the p95. Prepared answers went from 0 hits to 5 of 8
with none wrong. The detector prompt change took the detector from 79% to 100% on the labeled
utterances, and no false fire shows: a speculative fast pass the detector rejects is never sent.
Model cost per live hour stayed between $0.31 and $0.40 (about $0.50 with Deepgram); the held
passes add about $0.02 per live hour.

The 0.9 s / 1.5 s target is still missed. What is left is mostly the detector call itself
(about 1.5 s p50 from here, 1.2 s of it the model) and the ~260 ms round trip from this desk to
the US endpoints, which every call pays. No faster OpenAI model is as accurate (section 4).

**Not done: the interleaved before/after run and the benchmark run of fix 7.** OpenAI timed out
from this machine for over an hour in the evening of 2026-10-14 (first-token timeouts on most
detector and fast-pass calls), so both attempts measured the outage and were discarded. Command
to rerun, once the provider probe is back near 1.5 s:
`uv run copilot-bench run --label final --runs 3 --python <head-checkout>/.venv/Scripts/python.exe
--also baseline-interleaved=<checkout of 6a5ca6b>/.venv/Scripts/python.exe`.

## 1. The benchmark

`evals/bench/` (evals/README.md, "Latency benchmark"): a 4 min 15 s mock interview spoken by a
synthetic TTS voice (`gpt-4o-mini-tts`, so no consent question), 21 labeled utterances: small
talk, a team introduction, behavioral and technical questions, a question with a 0.9 s
mid-sentence pause, a quick follow-up 3 s after a question, two flat questions read with falling
intonation, an unfinished question the user forces with the hotkey, and the two non-questions
that fired in the live test (a request to share the screen, "what is that?"). The candidate is
fictional, with a hot digest and a pack of 12 prepared answers, 5 of which the script
paraphrases. `copilot-bench run` replays it through the real gateway and providers with
`copilot-replay`'s client-side VAD (46 speech-end frames: the 200 ms hangover splits sentences at
commas, as the desktop client does), consent off, a cold prompt cache per run, and scores:
per-stage p50/p95, first word at the client from the true end of speech, detection precision and
recall over the labeled utterances, speculative waste, prepared-answer hits, and cost per live
hour (every model call the session logged, plus Deepgram at $0.0077/min list).

**Read the numbers with three caveats.**

1. **Provider latency drifts by the minute.** Every run records a probe of five raw detector
   calls. It ranged from 1.2 s to 2.1 s on a normal afternoon and reached 8 s during an outage.
   Labels run at different times are not strictly comparable; the final comparison (section 3)
   alternates old and new code run by run.
2. **This desktop is ~260 ms from the US API endpoints** (ping to api.openai.com and
   api.anthropic.com). A streamed request pays at least one round trip before its first byte, so
   every detector and fast-pass number here carries 0.25 to 0.5 s that a gateway in us-east-1
   would not.
3. **Two runs per fix** are enough to see the large effects (fixes 1, 2, 2b, 5, 7), not
   100 ms ones. One run of fix 6 hit a network outage (12 failed Deepgram handshakes, OpenAI
   first-token timeouts) and is marked below.

## 2. Per fix

Each column is the code at that commit, 2 runs (baseline 3). First word is at the client from
the true end of speech (VAD hangover included, overlay render not); stages are in ms.

| | baseline | fix1-held | fix2-one-call | fix2b-no-glue | fix3-no-wait | fix4-prewarm | fix5-thresholds | fix6-claude-warm |
|---|---|---|---|---|---|---|---|---|
| Runs | 3 | 2 | 2 | 2 | 2 | 2 | 2 | 2 |
| Provider probe, detector model (ms) | 1891 | 1398 | 1632 | 1266 | 1516 | 1484 | 1438 | 1218 |
| First word p50 (ms) | 5047 | 3861 | 2791 | 2276 | 2345 | 2406 | 2280 | 2958 |
| First word p95 (ms) | 9246 | 7444 | 6087 | 3221 | 3184 | 3578 | 4300 | 8320 |
| Server first word p50 / p95 | 4820 / 9031 | 3640 / 7228 | 2562 / 5861 | 2047 / 2999 | 2108 / 2954 | 2172 / 3349 | 2047 / 4061 | 2725 / 8096 |
| Detector p50 / p95 | 1812 / 4700 | 1860 / 4719 | 1827 / 4123 | 1500 / 2366 | 1514 / 2487 | 1530 / 2367 | 1750 / 2964 | 2116 / 2971 |
| Turn confirmed p50 / p95 | 3452 / 7170 | 3569 / 6206 | 2562 / 5861 | 2047 / 2990 | 2108 / 2954 | 2172 / 3349 | 2047 / 3676 | 2725 / 8096 |
| Fast start p50 / p95 | 3452 / 7170 | 1944 / 2485 | 827 / 1688 | 562 / 1555 | 483 / 882 | 483 / 1293 | 405 / 1072 | 508 / 5161 |
| Fast TTFT p50 / p95 | 1242 / 2132 | 1094 / 2301 | 1359 / 2382 | 1156 / 1562 | 1178 / 1776 | 1077 / 1869 | 1164 / 2400 | 1500 / 2465 |
| Deep first word p50 / p95 | 6671 / 11871 | 6160 / 12921 | 5269 / 10244 | 4343 / 8796 | 4495 / 13282 | 4330 / 8574 | 4050 / 6063 | 5886 / 11665 |
| Forced first word p50 | 891 | 1070 | 1868 | 1304 | 812 | 1546 | 1718 | 2929 |
| Precision / recall | 0.889 / 0.667 | 0.9 / 0.75 | 0.938 / 0.625 | 0.895 / 0.708 | 0.895 / 0.708 | 0.938 / 0.625 | 0.938 / 0.625 | 0.857 / 0.5 |
| False fires per run | 1.0 | 1.0 | 0.5 | 1.0 | 1.0 | 0.5 | 0.5 | 1.0 |
| Shown then cancelled per run | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| Detector calls per run | 51.3 | 56 | 58.5 | 59.5 | 60.5 | 49.5 | 59 | 52 |
| Speculative waste | 0.0 | 0.711 | 0.763 | 0.7 | 0.712 | 0.727 | 0.753 | 0.771 |
| Prepared hits (correct / chances, wrong) | 0 / 7, 0 | 0 / 6, 0 | 0 / 5, 0 | 0 / 6, 0 | 1 / 6, 0 | 2 / 5, 0 | 5 / 8, 0 | 3 / 5, 0 |
| Model cost per live hour | $0.34 | $0.40 | $0.37 | $0.39 | $0.38 | $0.35 | $0.31 | $0.36 |
| Total cost per live hour (with STT) | $0.51 | $0.57 | $0.54 | $0.56 | $0.55 | $0.52 | $0.48 | $0.53 |

fix6-claude-warm: run 2 hit a network outage (12 failed Deepgram handshakes, OpenAI first-token timeouts); its p95 and recall reflect that, not the change (Claude-only).

| Fix | Commit | What changed | Effect on the benchmark |
| --- | --- | --- | --- |
| Benchmark and measurement hooks | a796577 | `copilot-bench`; every detector decision logged (text hash, decision, type, confidence, latency, tokens, cost; text only with consent); cancelled passes logged with estimated cost | The logging half of issue 8 came first: the baseline needs detector cost, and the misses could not be explained without it |
| STT reconnect crash | 6a5ca6b | Found by the benchmark: a Deepgram reconnect during speech crashed the session (4500) | No latency effect; the fix 6 outage run survived 12 reconnects |
| 1. Fast pass held, in parallel with the detector | 9fbfe6f | Starts at the speech end when a local check passes; output held on the gateway until the detector confirms; discarded silently otherwise | First word p50 5.0 to 3.9 s, fast start p50 3.5 to 1.9 s. Waste: about 70% of held passes are discarded (comma-split fragments, statements with question words), $0.0012 a run, **about $0.02 per live hour** |
| 2. One detector call per turn | 1278423 | The final replaces the interim's call the moment it arrives, when it changes the words | p50 3.9 to 2.8 s, p95 7.4 to 6.1 s. Detector calls per run did not fall (the cancelled interim call is still billed, about $0.0001 each); the back-to-back wait did |
| 2b. Utterances no longer glued together | 6f11b91 | Found by the benchmark: with the detector slower than the gap between sentences, a resumed speaker's turn kept the earlier text, so questions went out glued to the sentences before them | **p95 6.1 to 3.2 s**, p50 2.8 to 2.3 s, recall up. Likely behind some live-test misses and false fires |
| 3. No fixed 150 ms wait for the prepared match | 8c177d5 | The model starts at once; a prepared hit before its first text takes over | Forced first word 1.3 to 0.8 s (n = 2, noisy); fast start p95 1.6 to 0.9 s. Automatic questions unchanged, as expected |
| 4. Prompt cache warmed at session start | 774a184 | OpenAI `prompt_cache_breakpoint` (explicit mode) and `prompt_cache_options.prewarm` on GPT-5.6+; Claude `max_tokens: 0` | Not visible on this script: since fix 1 the held passes on the opening small talk already warm the cache. Measured directly: a cold gpt-6-luna fast pass took 5.3 s to its first token, warm 1.0 s. Costs $0.00018 a session |
| 5. Prepared thresholds per embedding model | dd9052f | Calibration set in `evals/prepared/`; text-embedding-3-large: 0.63 with a 0.05 margin (0.85 kept for Cohere) | **Prepared hits 5 of 8, none wrong** (0 of 7 at baseline); model cost per live hour $0.38 to $0.31 |
| 6. Claude clients: keep-alive and warm-up | 7b318a3 | 120 s keep-alive and a warm-up on the Claude API and Bedrock clients | Not exercised (OpenAI only). Measured directly: a cold Claude API connection took 4.6 s, warm 0.45 s. Run 2 of this label hit a network outage |
| 7. Detector prompt | 1bbe65c | The prompt names the call type; in a one-on-one call a question from the other side is for the user; requests, audio checks and short reactions need no answer | Detector eval: 79% to 100% (3 repetitions). See section 3 for the benchmark |

## 3. Before and after, interleaved

Pending (see the summary): two attempts on 2026-10-14 ran into an OpenAI outage from this
machine and were discarded. The per-fix table above is the before/after until then; fix 7's effect
is measured on the detector eval (79% to 100%), not yet on the full benchmark.

## 4. The remaining floor: the detector call

After these fixes the first word is about `max(detector, fast-pass first token)` plus the
silence and STT timings, and the detector is the larger term: about 1.5 s at p50 from this desk
(1.2 to 1.3 s of it is the model, the rest the round trip).

**Faster OpenAI models, measured.** `copilot-detector-eval` on the benchmark's 22 labeled
utterances (20 scored), 3 repetitions each, the shipped detector prompt, each model at its
lowest reasoning setting, from this desktop:

| Model | Reasoning | Accuracy | Type accuracy | p50 (ms) | p95 (ms) | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| gpt-6-luna (current) | none | **100%** | 94% | **1,508** | 3,164 | none |
| gpt-5.6-luna | none | 100% | 100% | 1,680 | 3,250 | none |
| gpt-5.4-mini | none | 100% | 100% | 1,688 | 4,582 | none |
| gpt-4.1-mini | n/a | 100% | 92% | 1,969 | 4,453 | none |
| gpt-4.1-nano | n/a | 75% | 100% | 1,601 | 2,995 | 25% misses |
| gpt-5.4-nano | none | 95% | 94% | 2,461 | 5,652 | 3 misses |
| gpt-5-nano | minimal | 57% | 73% | 1,329 | 6,254 | many misses, 1 false fire |

No OpenAI model available to this key is faster at the same accuracy. With about 40 output
tokens, the time goes to fixed per-request overhead and the round trip, not to model size, so
the smaller models do not help. (The current prompt also matters: with the old prompt,
gpt-6-luna scored 79%.)

**Other options, not built (they need your OK):**

| Option | Expected first word | Cost and risk |
| --- | --- | --- |
| **Run the gateway near the API** (Stage 0 on AWS us-east-1, as planned) | About −0.3 to −0.5 s on every detector and fast-pass call, from the measured 260 ms round trip | No code. The single largest remaining gain; the numbers in this report are from a desktop |
| **Shorter detector output** (a two-field schema, e.g. `decision: answer|none|incomplete` and `type`) | About −0.2 to −0.3 s: the detector's JSON finished about 0.4 s after its first token (median, raw probe) | A prompt and parser change; measure on the detector eval first |
| **Claude Haiku 4.5 (or 5.5) as detector only** | The design assumed about 0.3 to 0.5 s for this call | Needs an Anthropic or Bedrock key, which the live setup did not use; the code already supports a per-job provider order |
| **Local stage-1 classifier** (as in the design: a small text model on the gateway CPU, ONNX, 10 to 30 ms) | Could fire high-confidence questions without waiting for the model, and drop most non-questions before any call | Needs a labeled corpus (the recorded call corpus does not exist yet; synthetic data plus consented live logs could start it). A wrong fire shows a wrong answer, so the model detector would still confirm, and only the "release" would move earlier: that brings back visible false starts unless they can be retracted |
| **Heuristic release** (release the held pass on a strong local signal, a "?" plus a question word, before the detector) | About −1 s on the clearest questions | Every heuristic false positive shows an answer that is then cancelled: the user asked that false fires never show |

**Recommendation.** Keep gpt-6-luna. Run the next live test from the AWS deployment, or at
least note the round trip, since this desktop adds 0.25 to 0.5 s per call. Then try the shorter
detector output on the detector eval (cheap, no model change). If p50 still misses 0.9 s after
that, the realistic route is a faster detector model on another provider (Claude Haiku for the
detector only) or the local classifier once there is a corpus to train it on. OpenAI alone does
not reach 0.9 s from here.

## 5. Spend

About $1 in total on OpenAI and Deepgram for this work (TTS $0.03; about 0.03 to
0.04 USD per benchmark run, 24 runs including the ones discarded in outages; detector evals and probes under $0.10), against the $5 cap.
