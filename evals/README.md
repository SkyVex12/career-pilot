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

## Session eval

`copilot-session-eval` scores whole calls on what went wrong in real ones. `sessions/*.toml`
are scripts paraphrased from the sessions of October and November 2026
(docs/analysis/2026-11-11-multi-session.md): each line has its time, its speaker and its
labels (should a card show, for whom the turn was, the type, whether the speaker stops
mid-sentence, what the answer must and must not say). Everyone in them is fictional and no
line is a real person's words. A standup fixture also holds the work items its pack is
rendered from.

| Part | What it measures | Needs |
| --- | --- | --- |
| `flow` | The script replayed through a real gateway with scripted speech to text and models; the detector answers from the labels. So it scores the gateway's own turn handling: one card per question, none where none is due, the whole question on the card (a first half before a pause too), follow cards only where nothing answered and the user is answering, the update card when the user starts their standup update | Nothing: offline, runs in the tests |
| `detect` | Each labelled line of the other side sent to the configured detector as the gateway sends it (the transcript before it, the turn-taking block in standups): fired against missed, false fires by addressee, type accuracy, latency | The detector model, about $0.0001 a call |
| `answers` | The lines with a `check` answered through the gateway's prompt builders and routes; every pattern is one check. `--deep` checks the detailed answer too | About $0.0003 a short answer, $0.006 a detailed one |

```sh
uv run copilot-session-eval run --label baseline --reps 2 --deep   # about $0.06
uv run copilot-session-eval run --label try --part flow            # offline
uv run copilot-session-eval compare baseline try                   # markdown table
```

Results go to `sessions/results/<label>/summary.json`, with every failed check (the answers
in it are about the fictional profile). To add a call: paraphrase its turns into a new
`.toml`, label them, and run `--part flow` first.

## Company workspaces

`copilot-company-eval` grades standup and meeting answers at a company the user works at.
`company/company.toml` is the benchmark candidate's current employer (fictional): two weeks of
work logs and three documents; `company/questions.toml` has 8 standup and 12 meeting questions,
each with key points and the facts it must not claim.

```sh
uv run copilot-company-eval prepare --database-url postgresql://postgres:...@host/postgres
uv run copilot-company-eval run --label company-before --variant before --reps 2
uv run copilot-company-eval run --label company-after --variant after --reps 2
uv run copilot-company-eval compare company-before company-after
```

- `prepare` runs the worker's own extraction (`worklog` and `knowledge` jobs, `.env` models)
  on the material in a scratch database (document facts are kept at once), and saves
  the company's memory to `company/memory.json` (committed, so runs need no extraction).
- `run --variant after` loads that memory into a scratch database, renders the standup and
  meeting packs with `copilot_core.workplace` (the meeting with its title, agenda and the facts
  that match it), and gives each pass the `<memory>` lines live search returns (full text for
  the fast pass, hybrid with the question's vector for the deep pass), through the gateway's
  prompt builders. `--variant before` is the same call before company workspaces: the career
  digest only, and an empty standup pack.
- The grader sees the company's material as written (`<company_truth>`), so any fact about the
  user's work or the company that the material does not hold counts as invented.

A run costs about $0.65 (80 answers, graded); `prepare` about $0.50. Results go to
`company/results/<label>/`, with the packs the after-run used (`packs.md`).

`copilot-bench run --company --database-url ...` replays the benchmark audio as a meeting at the
same company: the gateway renders the company's pack at session start and searches its memory
(in a scratch database) for every question, and the summary adds the memory stages
(`memory_wait_ms`, `memory_fts_ms`, `memory_hybrid_ms` and the deep pass's). `--user-id` names
the user the gateway signs in, for a gateway with Google sign-in (the uuid5 of the token's sub).

## Engineering context

`copilot-knowledge-eval` checks whether standup answers handle technical questions from a
company's documents. `knowledge/questions.toml` holds the 35 technical turns of the standups
analysed in docs/analysis/2026-11-14-standup-technical.md, in order, moved to a fictional
company (its four documents are in `knowledge/docs/`, its recent work in `[[item]]`), plus two
questions no document answers. Every line is invented wording. Each question has the call's
lines before it, the detector types that give the right shape, the document that holds the
fact, a pattern the prompt must contain for the fact to count as retrieved, and the patterns a
correct answer does and does not contain.

Each question runs the live path's pieces in order: the detector prompt on its route; the
company's pack and digest, memory search and section search; the short and the detailed
answer on their routes. A question the detector does not fire on gets no answer, as in a
call, and counts as wrong.

```sh
uv run copilot-knowledge-eval run --label after --database-url postgresql://postgres:...@host/postgres
uv run copilot-knowledge-eval compare before after
```

`run` extracts the documents once per label in a scratch database (kept in
`knowledge/results/<label>/prepared.json`, local only, so a rerun asks the questions again
without extracting) and writes `summary.json` and `answers.json`. A label costs about $0.10
for the questions plus the extraction (about 14k tokens on the extraction model).

The same file runs against another checkout's code, which is how `before` was made from
main: add a worktree of that commit, copy `knowledge/` and `knowledge_eval.py` into it, and
run `uv run python -m copilot_evals.knowledge_eval run --label before ...` there. Where
`copilot_core.knowledge` does not exist the documents go through that checkout's own
extraction with every proposal approved, and there are no sections and no digest.

| | before (main) | after |
| --- | --- | --- |
| Detected | 30/37 | 35/37 |
| Type right | 10/37 | 33/37 |
| Fact in the short answer's prompt | 19/31 | 31/31 |
| Fact in the detailed answer's prompt | 24/31 | 31/31 |
| Short answer correct | 19/33 | 29/33 |
| Detailed answer correct | 20/33 | 30/33 |
| Says it will confirm when nothing covers it | 2/2 | 2/2 |

One run each (2026-11-14, gpt-6-luna and gpt-6.1-sol); answers vary run to run by a question
or two. The sections' vector search is not part of this eval (its sections are not in the
database); keyword search and the digest are.

## Sensei answer-quality comparison

Run from the workspace after `uv sync`. This uses real model APIs and costs money; no
new infrastructure is provisioned. `fakeredis` and the gateway text-STT transport replace
storage and audio, while the worker pack builder and gateway answer pipeline are unchanged.
Keep the export and **all private artifacts outside the repository**. Documents are data,
never instructions. Neither candidate speech nor Sensei answers enter our gateway.

Prepare a private directory containing UTF-8 `resume.txt` (extracted from the supplied PDF)
and `jd.txt`. The observed export consists of four-line records: `#id`, timestamp,
interviewer text, suggested answer. The parser rejects malformed records without echoing
content. Confirm these field roles before using a different export. Dates need an explicit
year; timestamp semantics are not inferred.

```powershell
uv run python -m copilot_evals.sensei_benchmark --export 'C:\private\interview.txt' --private-dir 'C:\private\sensei-baseline' --year 2026 --max-cost 20
```

This is one sequential command: parse, prepare/cache the profile, replay all interviewer
turns at their original spacing, judge response-seeking turns in randomized/swapped A/B
order, diagnose losses separately, and write the paraphrased report/fixtures. The default
spend guard is $2.95 unless explicitly changed; `--max-cost` persists the chosen guard in
that private directory. Unknown or cancelled usage retains conservative reservations.
Do not run concurrent phases against one private directory. A 45-minute interview takes
45 minutes to replay. `--speed` changes timing and therefore makes a different experiment.
The live run uses the configured gateway routes; preparation currently uses the priced
`gpt-6.1-sol` extraction route. The independent judge is `gpt-6-astra`.

Phases can be resumed with `--phase prepare|replay|judge|report`. An incomplete replay
restarts from the beginning to rebuild conversation state; partial judgments are resumed.
Input hashes prevent reuse after the export, resume or JD changes. A fixed pack and
question labels can be reused for a later code change:

```powershell
uv run python -m copilot_evals.sensei_benchmark --export 'C:\private\interview.txt' --private-dir 'C:\private\sensei-after' --reuse-profile 'C:\private\sensei-baseline' --baseline 'C:\private\sensei-baseline' --year 2026 --max-cost 20 --report 'docs/analysis/sensei-after.md' --fixtures 'evals/sensei/after-paraphrased.json'
```

The comparison requires identical sources, pack, labels and judge. Report and comparison
files contain only paraphrases, IDs and measurements; the `.scores.json` fixture is the
numeric baseline. Exact reproduction requires the original private export, because
rewriting an interview question changes the experiment. The committed paraphrased
question fixture documents the eval cases without publishing the interview.

Private outputs include `turns.json`, `profile.json`, `replay.json` (timestamped gateway
messages), `answers.json` (fast/deep text, types, citations and stage latency), `grades.json`,
`summary.json`, `diagnosis.json`, and `cost.json`. Do not commit these. Console output is
metadata only; provider failures may contain source text and are not echoed.

Open private `human-review.html`, score the seeded 15-question A/B sample, and download
scores as `human-review.json` beside it. The HTML includes resume/JD evidence and no model
grades or assistant identity labels. Then run the same command with `--phase report` to
calculate human/judge preference agreement and per-criterion mean absolute error. Grades
remain provisional until all 15 are scored, preference agreement is at least 80%, and each
criterion's MAE is at most 0.75. Calibration covers initial answers; expanded-answer grades
remain model-only. Order disagreements are reported separately, never converted to ties.

The report includes coverage-inclusive scores (missing answer = 1), conditional scores,
per-type scores, unsupported personal-claim counts, timing from text injection, and ten
largest losses. Export timestamps do not establish question-end time; no STT or Sensei
latency claim is made. Only suggested responses present in the export are observed, so
Sensei's coverage is 100% by construction, not measured recall over an entire call.
