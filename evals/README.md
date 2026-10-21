# evals

Eval sets, the recorded call corpus and their runners (ARCHITECTURE.md, "Testing and evals").
Recorded calls hold people's voices: only add recordings that every participant agreed to,
and keep raw audio out of git.

## copilot-replay

Streams a WAV file through the gateway in real time as the "them" channel, the way the
desktop client would: Silero VAD (`pysilero-vad`, no torch) decides speech start and end,
only speech is sent in 100 ms frames, with a 200 ms pre-roll and a speech-end frame after
`--min-silence-ms` of silence.

```sh
uv run copilot-replay clip.wav [--url ws://127.0.0.1:8081/v1/session] [--json run.json]
```

It prints the transcript, detected questions and streamed answers as they arrive, then a
table per question and the p50/p95 of each stage:

| Column | Meaning |
| --- | --- |
| `vad` | VAD hangover: true end of speech to the speech-end frame (client side) |
| `det` | Detector call (server, `latency_ms.detector_ms`) |
| `turn` | Speech-end frame to the question being confirmed (server) |
| `ttft` | Fast-pass request to its first token (server) |
| `srv1st` | Speech-end frame to the first `answer.delta` sent (server) |
| `1st word` | True end of speech to the first `answer.delta` received (client), in seconds |
| `deep1st` | True end of speech to the first deep-pass delta, in seconds |

First-word p50/p95 is compared with the 0.9 s / 1.5 s target. `--drop-at SECONDS` aborts the
socket mid-call and resumes with the 10 s ring buffer, as the desktop client does. This is the
replay tool for the recorded call corpus.

## Latency benchmark

A scripted mock interview replayed through the real gateway with real providers (OpenAI and
Deepgram, from `.env`), to measure live-answer latency and detection before and after a change.
Everything in it is synthetic: the interviewer is a text-to-speech voice and the candidate is
fictional, so no recording consent is involved.

| File | What it is |
| --- | --- |
| `bench/mock_interview.toml` | The script and its labels: each utterance's text, the silence before it, the TTS voice and delivery (a flat, falling delivery for the "no rising intonation" questions), and what should happen (`answer`, `none`, or `either`), the question type and the prepared answer it paraphrases. It covers small talk, behavioral and technical questions, a question with a mid-sentence pause, a quick follow-up, flat questions, and the non-questions that fired in the 2026-10-07 live test (a request to share the screen, "what is that?") |
| `bench/profile.toml` | The fictional candidate: hot digest, ranked stories and 12 prepared answers |
| `bench/audio/` | Generated audio and timeline (gitignored; `copilot-bench-audio` rebuilds it) |
| `bench/results/<label>/summary.json` | Each benchmark's summary (raw runs and gateway logs stay local) |

```sh
uv run copilot-bench-audio                     # once, a few cents of TTS; cached per line
uv run copilot-bench run --label baseline --runs 3
uv run copilot-bench run --label fix --runs 3 --also base=../base-checkout/.venv/Scripts/python.exe
uv run copilot-bench compare baseline fix      # markdown table
```

`run` starts the gateway from this checkout on port 8091 with its own user id (`BENCH_USER_ID`),
writes the profile's digest and pack to Valkey for that user (prepared answers embedded with the
configured model), and replays the audio with consent off, so the exact-repeat cache cannot
answer from an earlier run. Each run gets a new digest build line, so its first question sees a
cold prompt cache as a new session would (`--warm-cache` keeps it). `--env KEY=VALUE` passes a
setting to the gateway; `--also LABEL=PYTHON` runs another checkout's gateway too, alternating
run by run, so both see the same provider conditions. Before each run a probe times five raw
detector calls: provider latency drifts by the minute, so every result carries it.

What a run reports, per stage (p50/p95 over the automatic questions, pooled across runs):

| Stage | Meaning |
| --- | --- |
| `detector_ms` | The detector call that decided |
| `final_ms` | Speech-end frame to the STT final |
| `turn_end_ms` | Speech-end frame to the question being confirmed |
| `start_ms` | Speech-end frame to the fast pass starting |
| `first_token_ms` | Fast-pass request to its first token |
| `server_first_word_ms` | Speech-end frame to the first answer text sent |
| `first_word_ms` | True end of speech in the audio to the first answer text received by the client: what the user waits for, less the overlay render (under 50 ms, not measured) |
| `deep_first_word_ms` | The same for the deep pass |

It also reports detection precision and recall over the labeled utterances (an `answer`
utterance counts once however many speech-end frames it has), questions shown and then cancelled,
false answers shown, speculative waste (fast passes started and thrown away, and their cost),
prepared-answer hits (right and wrong), detector calls, and cost: every model call the session
logged (answers, detector calls, cancelled passes with an estimate, cache prewarms) plus Deepgram
for the audio streamed ($0.0077 per minute list price; `--stt-price`), per run and per live hour.
A run costs about $0.03 to $0.10, mostly the deep pass.

## Prepared-answer calibration

`prepared/calibration.toml` holds paraphrases of the benchmark profile's 12 prepared questions
(should match) and 30 questions that must not match, many on the same topics. The script embeds
them with the configured model and scores every threshold and margin over the runner-up:

```sh
uv run copilot-calibrate-prepared --json prepared/<model>.json
```

It recommends the pair with the best recall among those with no wrong match, plus 0.03 on the
threshold, since a wrong match shows points for a different question. The result goes into the
gateway's `prepared_answer_thresholds` default for that model id.
`prepared/openai-text-embedding-3-large.json` is the October 2026 run.

## Answer quality

`copilot-answer-eval` runs the gateway's own fast and deep prompt builders and model routes
(`.env`) on 26 questions, with the fictional profile of `bench/profile.toml` as digest and pack:

- the benchmark's 12 questions that need an answer, each with the 60 s of transcript before it;
- the 14 of `answers/questions.toml`, which the profile does not cover: general technical
  (idempotent payments, Kubernetes probes, isolation levels, Kafka exactly-once), system design
  (a ledger, notifications at scale), behavioral with no matching story (learning on a
  deadline, bad news, time zones), "other" (why this company, salary, culture, five years) and
  coding. Each lists the key points of a complete, correct answer and the personal facts the
  profile does not hold, for the grader.

Three of the benchmark's questions (process vs thread, database index, typing a URL) are
general knowledge too; with the 14 they form the "not covered" group, summarized apart. The
fast pass gets no question type, as a held pass does; the deep pass gets the labeled type and
no memory block, as in Stage 0. A model (`--grader`, default gpt-6.1-sol) grades each answer
without knowing which prompt wrote it:

| Criterion | Meaning |
| --- | --- |
| grounded | Every point about the candidate cites a short id whose entry supports it (general knowledge, a plan, and facts from a section without an id need none) |
| invented | Personal facts the digest and pack do not hold, general knowledge passed off as the candidate's own experience ("at Larkspur we ran Kubernetes"), a salary figure given as theirs, and facts about the company beyond the job summary and transcript |
| complete (1 to 5) | The notes carry a full answer to the question asked, against its key points when it has them; 1 is thin or evasive ("not in my notes", only how one might approach it) |
| correct (1 to 5) | The general knowledge in it (technical facts, design, advice) is right |
| spoken (1 to 5) | Short cues in the candidate's own wording to speak from, against polished prose, filler and stock phrases |
| shape (1 to 5) | The right length and shape for the pass and question type (the deep shapes of `prompts.DEEP_SHAPES`) |

Plain counts need no model: words, words per line, lines with a citation, citations of short
ids the profile does not have, stock phrases from a fixed list, and full written sentences.
Each run adds up its cost (answers and grades, at `COPILOT_MODEL_PRICES`).

```sh
uv run copilot-answer-eval run --label before --reps 2    # about $0.80: 104 answers, graded
uv run copilot-answer-eval run --label try --reps 1 --only salary --only ledger   # a few
uv run copilot-answer-eval regrade before after           # the same rubric over saved answers
uv run copilot-answer-eval compare before after           # summaries and 6 examples side by side
```

Results go to `answers/results/<label>/`: `answers.json`, `summary.json` and `samples.md`, the
answers and grades to read. The shape grade judges against the current `DEEP_SHAPES`, so
regrade older labels after changing them.
