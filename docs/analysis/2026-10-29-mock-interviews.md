# Mock interviews, 2026-10-29: a session that outlived its call, a call in Russian

Four interview sessions ran through the app on 2026-10-29, on three linked Career-OS profiles of
one account, from two computers, against the Stage 0 GCP deployment, desktop app 0.1.13, OpenAI
routes (gpt-6-luna for the detector, fast pass and summary; gpt-6.1-sol for the deep pass and the
post-call outputs), Deepgram Nova-3 on both channels. Transcript consent was on in all four. As
before, this document quotes no interviewer words and names no interviewer, candidate or hiring
company: findings are paraphrased, profiles are A, B and C, and hiring companies are described.

| Session | Profile | Started (UTC) | Length | Backend (deployed main) | Computer | Call |
| --- | --- | --- | --- | --- | --- | --- |
| `01a18862-228a-7d4c-969b-78c53c226c5e` | A | 15:58:15 | 53.4 min | `bee5ac9` | 1 | Technical round, .NET, AWS and React, with a recruiter |
| `01a18897-28c9-7a4f-ba87-0b4cc019c36e` | B | 16:56:10 | 92.6 min | `bee5ac9`, then `e76182b` from 17:22, `8ba9d36` from 18:18 | 2 | Screen for a full-stack role at a medical-device startup (its first 30.5 min) |
| `01a188b5-b9a4-7d05-93f1-4cfaf78a49f5` | A | 17:29:33 | 59.2 min | `e76182b`, then `8ba9d36` from 18:18 | 1 | Interview at a software outsourcing firm, held in Russian |
| `01a188d1-54cf-7680-8b0f-0515710e3278` | C | 17:59:42 | 13.0 min | `e76182b` | 2 | Recruiter screen for a senior Drupal front-end role |

The computers are told apart by the address each session's WebSocket closed from (the proxy log).

## Summary

The cross-session picture matters more than any one call, so it comes first.

**What failed on more than one profile** (A's first call had no data and A's second no
recognizable speech, so the answer findings rest on B and C):

- **Questions that split across turns.** An interviewer who pauses mid-question gets two to four
  answers, each cancelling the last one's deep pass: 6 questions on profile A became 15 fires, and
  one each on B and C. One fast answer on A answered the previous topic.
- **The deep pass on things that need none.** Yes-or-no screening checks (work authorization,
  sponsorship, worked here before, relatives here, a degree's school) got a full deep pass, about
  $0.006 and 4 to 10 s each, which 7 times came back as an odd clarifying question. Four fast
  passes stated personal facts the data does not have ("no, I haven't worked there"). 8 questions
  on B and C; A had none.
- **Deep first-token timeouts.** 7 in B's and C's own calls, each putting the deep answer's first
  word at 9.7 to 11 s instead of about 4 s (11 more, and 2 deep passes lost, in B's runaway part).
  The deep pass has no hedge.
- **Follow cards repeat what is on screen.** In B's and C's calls, 18 follow cards (a third of
  the cards) showed about 12 s after the question; 15 were on turns already answered and mostly
  restated that answer, each with its own deep pass (about $0.13 for all 18). On the turns
  nothing had answered they were the only help, and the right one.

**What failed on one profile or one computer:**

- **A session that kept running after its call (computer 2, profile B).** Profile B's session
  stayed live 62 minutes after its interview ended. It transcribed the Russian call and then the
  whole of profile C's interview, answered C's interviewer 29 times from B's resume (telling it B
  has no Drupal years to claim, and that B went to C's university), and stored C's interview,
  both sides, in B's workspace. The app has no single-instance guard and a session ends only
  after 10 minutes without speech; the gap between the calls was 7.2 minutes.
- **A call held in Russian (profile A).** After the interviewer asked whether the candidate
  spoke Russian, the next 45 minutes were in Russian. English-only speech recognition returned
  fragments ("Mhmm", "cloud provider"); the app answered 1 question in 59 minutes.
- **No digest, no pack (profile A, first call).** The gateway refused the selected application
  as another workspace's (the shared-people bug fixed by #38 an hour later), so every answer was
  general knowledge with no citation. Already fixed.
- **No mic transcript on computer 1.** Neither of profile A's sessions has a single line of the
  user's speech, so follow cards, earlier answers and the post-call feedback had nothing from the
  user. Profile B and C on computer 2 had 388 and 136 mic lines.
- **A wrong number (profile C).** Asked for years of Drupal, the fast pass said about thirteen;
  the digest's two Drupal roles add up to about seven, which the deep pass said.

**What worked.** Detection on the questions that came whole: 49 of 54 interviewer questions
fired, 2 missed (each covered by a follow card), 3 forced by the user, and one rhetorical false
fire. First word at the client 1.9 s p50 and 2.7 s p95 over 62 fires, the fastest of any
analysis so far. Grounding held: every cited claim matched its source except one attribution and
the one sum above, and the hands-on suggestions were marked `[~]`. On profile C the user spoke
almost word for word from the paragraph-style deep answer.

Cost about $1.56 for 218 live minutes (about $0.43 per live hour, against the $0.84 estimate),
but about $0.4 of it was profile B's session answering other people's calls; profile C's dense
13-minute screen ran at about $1.39 an hour. Speech recognition of the 45 minutes in Russian was
billed and not measured.

## 1. Data available

| Data | Where | State |
| --- | --- | --- |
| Session rows | `app.sessions` | All four: interview, Zoom, `store_transcript` true; profile A's first call has no application (the gateway dropped it) |
| Transcripts | `app.transcript_segments` | A1: 455 "them" (14.7 min of speech), 0 "me". B: 627 "them" (18.5 min), 388 "me" (17.9 min). A2: 56 "them" (1.3 min), 0 "me". C: 88 "them" (4.1 min), 136 "me" (6.6 min) |
| Questions and answers | `app.questions`, `app.answers` | 29, 55, 1 and 28 questions; 51, 106, 2 and 55 answers with text, `latency_ms` (with `client_first_word_ms`), tokens and cost |
| Detector decisions | Cloud Logging | 455, 669, 73 and 80 lines (decision, type, latency); token totals only in the session end line, which is **zero for B and A2**: both resumed on a new gateway task after a deploy, and the counters start again there |
| Packs and digests | Valkey | Kept: the pack each session start line names, and its digest (A1 had none) |
| Resume and job description | `app.applications` | The three applications have no paired resume version (`resume_version_id` empty): the digest is the profile's main resume, the pack is built from it and the job description |
| Summaries | Cloud Logging | 9, 26, 0 and 5 calls with tokens and stop reason; text gone |
| Post-call outputs | `app.session_outputs` | Summary, feedback, follow-up email for each |
| Computers | Proxy log | Session WebSocket close lines with the client address and duration |

Missing: the desktop's events (which overlay showed what, expand presses, whether two app windows
were open on computer 2), why computer 1 sent no mic speech, Deepgram billed audio per stream, and
the held passes' tokens (cost below estimated from the answers' token counts).

## 2. Per session

Times are from session start. "Shown" is the client's first word, from the question's end.

### Profile A, technical round (A1)

A tech lead asked about 20 questions over 45 minutes: API endpoint anatomy, validation, layering
business logic, testability, return types, when to merge services, AWS tooling for 50 services,
React structure, TypeScript, code review; then the recruiter explained the process.

- **No pack.** `pack=none digest=False`: the application belonged to another workspace's row at
  the time. Every answer was reasoning in "I'd" form, no citation, so nothing personal was said
  and nothing could be grounded. Fixed by #38; the session itself was moved to this profile
  afterwards.
- **Fragments.** The interviewer speaks slowly with one- to two-second pauses, and the "them"
  stream came as 455 short segments, many of one to three words. 15 of 18 questions fired, but 6
  questions became 15 fires in a row 1 to 25 s apart (validators; AWS tooling, 4 fires; React
  layout; TypeScript; strict typing; code review). Each new fire cancelled the previous deep
  pass, so 6 of those fires never got one, and the cards changed while the user talked. On the
  strict-typing question, the fast pass answered the earlier topic (out parameters and `dynamic`)
  because its fragment had too little text.
- **Missed, then forced.** Three questions spread over many fragments never fired (the request
  path and tracing opener, layering business logic, merging two services); the user pressed for
  an answer each time, which came at 0.9 to 1.3 s. One Respond press, on a remark, got a fine
  reply.
- **Types.** Right on all but one: endpoint anatomy as `system_design`. One rhetorical aside
  ("any benefit over void or dynamic?") fired as `technical`; harmless.
- **Answers.** Correct and specific (ASP.NET Core attributes, `IValidatableObject`, ECS on
  Fargate, discriminated unions). Fast cues 4 to 5 lines of 6 to 12 words; deep answers
  paragraphs.
- **Mic.** No user line at all, so nothing to compare the suggestions with.

### Profile B, full-stack screen (B, first 30.5 minutes)

A principal engineer asked 15 questions: why this role, remote work, collaboration, front end,
back end and databases, technology preference, AWS and deployment, EMR integration, unclear
requirements, regulated medical software, AI tools, and two eligibility checks; then the
candidate's questions and the process.

- **Detection.** 13 of 14 fired in time (detector 1.4 s p50); one split into two (back end, then
  databases); the EMR question was missed (the user said "yes" mid-question and the interviewer
  said "go ahead"), and its follow card 12 s later had the right material. One false fire: the
  interviewer's rhetorical worry about vendors training on their data.
- **Grounding.** Every cited claim matches the digest: the healthcare platform, the
  payer-provider portal, the dashboards, the pipelines. One attribution the digest does not make:
  the deep pass put the Java and .NET enterprise project at the earliest employer. On regulated
  device software both passes said plainly that the user had related compliance work, not FDA
  device work, which is what the user then said.
- **Suggested against spoken.** The user spoke in their own words and mostly matched the first
  cues (healthcare background, reusable components, schema and API design, iterating with
  stakeholders). The two eligibility checks were answered in under a second; the fast pass's
  "answer based on your situation" and a stray foreign-script token in one cue were noise.
- **Prepared answers.** 2 hits (AWS deployment at 0.77, questions for them at 0.66); the
  why-this-role question scored 0.616 against the 0.63 threshold.
- **Then the session kept running** (section 3).

### Profile A, interview in Russian (A2)

The interviewer asked, in English, whether the candidate spoke Russian; the app answered that one
("yes; continue in Russian?"). The rest of the call, about 45 minutes, was in Russian. English
Nova-3 returned 55 fragments and fillers, no mic lines, no question; nothing was shown after
minute 4. The session ended on idle 10 minutes after the call.

### Profile C, Drupal screen (C)

A recruiter asked 21 questions: why this role, why the last role ended, the school, biggest
achievement, how the user got into technology, ideal position, years of Drupal, a recent Drupal
site, several sites on one platform, a hard front-end bug, upgrades and migrations, questions for
them, the job search, and five eligibility and conflict-of-interest checks.

- **Detection.** 20 of 21 fired, detector 0.9 s p50; the upgrade-or-migration question split in
  two. Missed: how the search is going (a follow card covered it at 9.5 s). The first fire,
  "any questions before we start?", came after the user had already said no. Types right except
  "what brought you into technology" as `behavioral` (the motivation rule says `other`); the
  answer was fine either way.
- **Grounding.** Cited claims match the digest, with one error: the fast pass said about
  thirteen years of Drupal, citing three roles, one of them not a Drupal role; the two Drupal
  roles run February 2019 to May 2026, about seven years, which the deep pass said. The user
  said a number of their own. On the five screening checks the fast pass answered for the user
  (never worked there, no relatives or friends there, no designations), none of which is in the
  data; the deep pass returned a clarifying question each time.
- **Suggested against spoken.** The user waited through a filler and then spoke the deep
  paragraph nearly word for word on 8 questions (why this role, achievement, a recent site, the
  multi-site question, the hard bug, and others), at reading pace. The hard-bug story was a
  suggested example: every line of it was marked `[~]` (a component with stale content, missing
  cache tags on a render array), and the user told it as their own. That is the suggested-details
  policy chosen on 2026-10-28 working as designed; it also means a whole incident, not a detail,
  was offered.
- **Paragraph style.** The deep pass ran in the paragraph style: first word 3.1 to 6.4 s,
  done at 7.9 s p50, 14.2 s p95.

## 3. Cross-session findings

### A session that outlived its call answered other people's interviews

Profile B's call ended at 30:32. The session stayed live until 92:33:

| From | What the session heard | Answered as profile B |
| --- | --- | --- |
| 30:32 to 37:42 | Nothing (7.2 min) | — |
| 37:42 to 63:18 | The Russian call of A2 (another computer, same moment) on "them" | Nothing (fragments) |
| 64:31 to 81:16 | All of profile C's interview, both sides: the mic lines are C's words, as on C's own session | 29 questions, 54 answers, $0.23 |
| 81:16 to 92:33 | Nothing, then the 10-minute idle end | — |

Profile C's session ran from computer 2 at the same time, so both sessions took the same audio.
The desktop refuses a second session in one app (`Gate::begin_start`), so two app processes were
running there; there is no single-instance guard. Whatever opened them, the result: profile C's
interviewer got answers from profile B's resume ("no Drupal years to claim", "I worked at the
streaming company", "yes, I went to that university", which no data says), and profile C's
interview, both sides, now sits in profile B's workspace and history. The gateway cannot tell,
because a session ends only after `idle_end_s` (600 s) without speech, and both deploys today
also resumed it.

### Questions split across turns

| | A1 | B | C |
| --- | --- | --- | --- |
| Questions asked in several fires | 6 (15 fires) | 1 (2) | 1 (2) |
| Deep passes cancelled by the next fragment | 6 | 1 | 1 |

The turn logic is right to end a turn at a pause and fire on a complete-looking question; the
cost is that the next fire on the same question starts over, with only its own fragment as
`Question:`. On A1 that fragment was once three words and the fast pass answered the topic
before.

### Screening checks

8 yes-or-no checks of the user's own situation fired (B: 2, C: 5, plus the school on C): the
user answered every one in 0.6 to 1.2 s, before any card. The fast pass either stated an answer
the data does not have (C: 4 times) or said "answer based on your situation"; the deep pass
(about $0.0067 each, 3.7 to 10 s) returned a question back each time. The prompt already says a
personal detail the data lacks is the user's to say; the fast pass did not follow it on these.

### Follow cards

| | B (own call) | C |
| --- | --- | --- |
| Follow cards | 11 | 7 |
| First word from the question's end | 12.2 s p50 | 13.1 s p50 |
| Of them, on a turn whose answer was already shown | 9 | 6 |
| Cost (fast + deep) | $0.084 | $0.045 |

On answered turns the follow card restated the shown cues (on C, three times almost word for
word). On the three unanswered turns (B's missed EMR question and an explanation that ended in
"go ahead", C's job search) it was the only help and had the right content.

### Grounding and shape, all sessions

| | A1 | B (own call) | C |
| --- | --- | --- | --- |
| Citations checked against the digest | 0 (no data) | 63 | 70 |
| Wrong | — | 1 attribution (the enterprise project's employer) | 1 (the years) |
| Personal facts not in the data, unmarked | 0 | 0 | 4 (screening) |
| Fast and deep disagree | 1 (strict typing, the fast pass off topic) | 0 | 1 (the years) |
| Used what the user said earlier | no mic lines | yes (AWS services, the medical-device answer) | yes (games, the multi-site question) |

Fast cues are glanceable (4 lines of 6 to 12 words). Deep answers ran in the paragraph style on
all four sessions (120 to 180 words), the Session tab's choice.

### Latency at the client

From the question's end, detected questions only (forced and follow left out); "deep done" is
the deep pass's end at the gateway.

| Session | n | First word p50 | p95 | Deep done p50 | p95 | Detector p50 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 25 | 1,650 | 2,613 | 8,657 | 10,831 | 955 |
| B (own call) | 15 | 2,031 | 3,160 | 9,088 | 15,876 | 1,360 |
| A2 | 1 | 1,931 | — | 3,260 | — | 855 |
| C | 21 | 1,795 | 2,372 | 7,855 | 14,187 | 923 |
| **All** | 62 | **1,852** | **2,662** | **8,606** | **15,485** | |

The first word is within a second of the 0.7 to 1.2 s target's upper end on most questions; the
10-28 fast-pass hedge held (8, 17 and 2 hedges, no fast pass lost). The deep pass is slow: the
paragraph style, and first-token timeouts on gpt-6.1-sol that retried on the same route (first
word 9.7 to 11 s on 7 questions in the own calls; 2 deep passes failed outright in B's runaway
part).

### Errors and noise

| | A1 | B | A2 | C |
| --- | --- | --- | --- | --- |
| Fast hedges / fast route failures | 8 / 2 | 17 / 0 | 0 / 0 | 2 / 0 |
| Detector hedges / route failures | 6 / 7 | 22 / 3 | 2 / 0 | 0 / 1 |
| Deep first-token timeouts (passes lost) | 0 | 15 (2) | 0 | 3 (0) |
| STT drops | 1 | 2 stalls | 0 | 0 |
| Gateway restarts mid-session (deploys) | 0 | 2 | 1 | 0 |
| Echo lines dropped | — | 3 | — | 1 |
| Summaries at the 500-token cap | 2 of 9 | 3 of 26 | — | 0 of 5 |

Both deploys today replaced the gateway under live calls: the deploy at 17:21 cut profile B's
interview for about 30 s (the interviewer was explaining; nothing was lost), the one at 18:18
cut B and A2. Each session resumed, but the counters in the session end line restarted, so B's
and A2's end lines say 0 questions.

Speech recognition, mic channel (computer 2; keyterms were on): delivery as two nonsense words,
the latest employer's name as a phrase, cache tags, render array, Terraform, GitHub Actions,
microservices, two AI coding tools, EMR, and UI/UX all misheard; Drupal right on C's session
(it was a keyterm there) and wrong on B's (it was not). "Them" channel: A1's interviewer came in
one- to three-word pieces, with observability and request journey misheard.

### Cost

| Session | Answers (logged) | Detector | Held passes | Summaries | STT | Total | Per live hour |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 (53.4 min) | $0.137 | $0.040 | ~$0.015 | $0.003 | ~$0.11 | ~$0.31 | ~$0.35 |
| B (92.6 min) | $0.435 | ~$0.06 | ~$0.09 | $0.008 | ~$0.33 | ~$0.92 | ~$0.60 |
| A2 (59.2 min) | $0.012 | ~$0.007 | — | — | ~$0.01 + Russian speech, unmeasured | ~$0.03+ | — |
| C (13.0 min) | $0.186 | $0.007 | ~$0.01 | $0.001 | ~$0.10 | ~$0.30 | ~$1.39 |

Detector cost from the end line's tokens (A1, C) or the decision count at C's tokens per call
(B, A2); held passes from the model passes not stored as answers; STT from transcribed speech at
$0.0077 a minute, or $0.0090 with keyterms. Of B's $0.92, about $0.45 came after its own call.
C's rate is above the estimate because it answered 28 questions in 13 minutes (129 an hour
against 40 assumed), each with a deep pass ($0.0067 on average, 95% of the answers' cost),
follow cards and screening checks included. Re-runs for this analysis: none.

## 4. Ranked issues

Ranked by effect on the user in a real interview.

### 1. A session left running answers the next call, under the wrong profile

- **Evidence:** B's session outlived its call by 62 minutes, answered 29 of C's interviewer's
  questions from B's resume, and stored C's interview in B's workspace (section 3). 1 session,
  2 profiles affected.
- **Root cause:** two app processes could run on one computer (no single-instance guard), and a
  session ends only after 10 minutes with no speech on either side; the next call started 7.2
  minutes after the last one ended.
- **Fix:** (a) the desktop runs as a single instance: a second launch focuses the first window
  (tauri-plugin-single-instance; needs desktop 0.1.15). (b) The gateway ends an interview
  session that has gone quiet after the other side's goodbye: 90 s without speech once the
  last "them" turn was a closing (thanks, bye, have a good day), 10 minutes otherwise
  (`idle_end_after_close_s`). (c) With your OK, delete from B's session what came after minute
  30:30 (C's call and the Russian call), which is not B's; C's own session already has C's
  call. No data moves to another profile.
- **Expected gain:** a forgotten session ends within about 2 minutes of the goodbye instead of
  running into the next call. **Risk:** a call that pauses right after a goodbye-like remark
  ends early; the user starts a new session (the history keeps the first). The 90 s only starts
  after a closing.

### 2. A call in another language leaves the user with nothing

- **Evidence:** A2: 45 minutes in Russian, 55 fragments, 1 question answered in 59 minutes.
- **Root cause:** Deepgram runs with English only, and the prompts answer in English.
- **Fix:** when the "them" stream has 60 s of speech (from the client's speech flags) that
  yields under a fifth of the expected words, the gateway reopens both STT streams with
  Nova-3's multilingual mode (`language=multi`, which covers Russian) for the rest of the
  session, logs it, and the answer prompts get a rule to answer in the language of the question.
  Later: a language choice on the Session tab.
- **Expected gain:** a call that switches language keeps getting answers. **Risk:** multilingual
  mode is billed at a different rate and may not take keyterms (price and feature check before
  merge); it only switches after a minute of unrecognized speech. Backend only.

### 3. Screening checks get invented answers and a wasted deep pass

- **Evidence:** 8 checks on B and C; 4 fast passes stated personal facts the data lacks; 8 deep
  passes (about $0.047, 3.7 to 10 s), 7 of which returned a question back; the user answered
  each in about a second.
- **Fix:** in an interview, the detector types a yes-or-no check of the user's own situation
  (work authorization, sponsorship, worked here before, relatives or friends here, holds a
  certification, "is that right?" about a resume fact) as `none`: the user knows the answer and
  needs no card. Plus paraphrased cases in the detector tests and the new eval script.
- **Expected gain:** no invented personal answers, a quieter overlay, about $0.007 per check.
  **Risk:** a check that needed a careful answer (a sponsorship nuance) gets no card; Ask still
  works.

### 4. Questions in pieces lose their deep pass and their context

- **Evidence:** A1: 6 questions became 15 fires, 6 deep passes cancelled, one off-topic fast
  answer; B and C one each.
- **Fix:** a question that fires within 10 s of the previous one, with no user speech in
  between, is a continuation: its `Question:` is the previous question's text plus the new
  text, so the fast and deep passes answer the whole question.
- **Expected gain:** the last fire answers everything asked, and the deep pass that survives
  covers all of it. **Risk:** two separate quick questions merge; the merged text still holds
  both. Backend only.

### 5. Follow cards repeat the answer on screen and pay for a deep pass

- **Evidence:** 15 of 18 follow cards in B's and C's calls were on turns already answered,
  mostly restating them, at about 12 s; $0.13 for all 18. The 3 on unanswered turns helped.
- **Fix:** follow only turns with no answer shown (missed or undecided); on answered turns the
  deep answer already continues.
- **Expected gain:** fewer repeated cards, about $0.008 per follow card skipped. **Risk:** a
  long answer on an answered turn gets no new card; the deep answer is there.

### 6. The deep pass stalls without a hedge

- **Evidence:** 7 deep first-token timeouts in B's and C's calls, first word 9.7 to 11 s on
  those 7 questions; 11 more and 2 lost deep passes in B's runaway part.
- **Fix:** hedge the deep pass as the fast pass is (`deep_hedge_ms`, 3000): with no first token,
  the second route starts alongside on the Priority tier.
- **Expected gain:** deep answers at about 4 to 6 s instead of 10. **Risk:** a hedged deep call
  costs 1.75 times, on the few that need it.

### 7. A sum over roles came out wrong

- **Evidence:** C: thirteen years of Drupal against about seven.
- **Fix:** the digest gives each role's length ("Feb 2019 to Oct 2022, 3 yr 9 mo"), and a rule:
  a "how many years" answer uses only the lengths of the roles that fit, or the profile's
  headline figure, never a new sum. **Risk:** low; a few tokens in the cached prefix.

### 8. No mic transcript goes unnoticed

- **Evidence:** computer 1: 0 mic lines over 112 minutes in two sessions; nobody was told.
- **Fix:** once the other side has spoken for 2 minutes with no speech from the mic at all, the
  gateway sends the status "We can't hear your microphone" (as it does for echo). **Risk:** a
  silent listener sees it; it clears on the first mic speech.

### 9. Deploys restart live calls and reset their counters

- **Evidence:** two deploys under live sessions; a 30 s gap on B; B's and A2's end lines zero.
- **Fix:** `copilotctl deploy` waits for live sessions to end (at most 45 minutes, then
  proceeds) before replacing the gateway; the session's counters travel with its state, so a
  resumed session's end line has the whole call. **Risk:** a merge's deploy can wait.

### 10. Smaller

- **Prepared answers rarely match:** 2 hits in 62 questions; questions with a preamble score just
  under the threshold (0.61 to 0.63). Match on the question's last sentence too. Needs a
  calibration re-run.
- **Paragraph deep answers and whole suggested stories:** the user read them aloud on C, a
  `[~]` incident included. Your policy from 10-28; no change proposed unless you want suggested
  stories limited to one marked detail.
- **Summaries near the cap** on long calls (B: 420 to 494 of 500 tokens, 3 at the cap): raise
  `conversation_summary_tokens` to 700 with the same 225-word target.
- **Cost estimate:** add this batch next to the 10-28 line in ARCHITECTURE.md, "Cost estimate".

### Recommended order

1 first: it put one person's interview in another's workspace and gave a wrong-profile
answer to a real interviewer. Then 3 and 4 (wrong or broken answers on every profile), 2 (a
whole call lost), 6 and 5 (deep answers late or repeated), 7 and 8 (small), 9 (operations).
Only 1(a) needs a desktop release; the rest are backend, worker and infra.

## 5. What this branch changed

Your call after reading section 4: better interview answers (the fast and detailed answers read
as unrelated, and the fast ones were sometimes off topic); follow the user only when no answer
was shown; the other quality findings; a microphone and audio test before a call; several
sessions at once on separate computers without one taking over another's call; an end after
the goodbye; and the stray part of profile B's session deleted. Deploys keep going out at
once (no waiting for live calls). The language switch (issue 2) is not in this branch.

| Fix | Commit | Tests |
| --- | --- | --- |
| Follow cards only on turns nothing answered (5) | Follow the user only on turns nothing answered | `test_no_follow_card_on_a_turn_that_was_answered`, `test_the_answer_follows_the_user_when_no_question_fired` |
| Fast and detailed answers linked; specific cues; every part answered; no screening cards or answers; role lengths (3, 7) | Answers: the detailed answer develops the cues on screen... | `test_the_deep_answer_develops_the_cues_in_the_chosen_style`, `test_fast_cues_are_specific_and_answer_every_part`, `test_a_yes_or_no_screening_check_needs_no_card`, `test_each_role_states_its_length` |
| A question in pieces is answered whole (4) | A question that goes on after a pause is answered whole | `test_a_question_that_goes_on_after_a_pause_is_answered_whole`, `test_a_question_continued_after_a_pause_joins_the_one_before` |
| Deep-pass hedge on Priority (6) | Deep pass: hedge a stalled first token on the Priority tier | `test_the_deep_pass_hedges_on_the_priority_tier` |
| End after the goodbye (1b), "can't hear your microphone" (8), totals across a resume (9, the logging half) | Sessions: end an interview soon after the goodbye... | `test_an_interview_ends_soon_after_the_goodbye`, `test_no_early_end_without_a_goodbye_or_outside_an_interview`, `test_the_badge_says_when_the_mic_is_not_heard`, `test_the_end_line_counts_the_whole_call_across_a_resume`, `test_a_goodbye_is_told_from_an_opening` |
| Audio test before a call; one app per computer (1a) | Desktop: test the microphone and call audio before a call; one app per computer | Rust `audio::check` (4 tests), `tests/audiotest.test.ts` |
| Fixtures for later batches | Evals: the 2026-10-29 mock interviews as paraphrased fixtures; Evals: report the types the detector got wrong... | `test_mock_interview_script_scores_the_10_29_turns` |

**How the answers changed.** The detailed answer was told to "go beyond the cues and never
restate one"; that rule, from the 10-28 complaint that it repeated them, is what made it switch
to other points. It now develops the cues on screen in their order, says more than each, and
puts a right point in place of a wrong one. The fast cues must each be specific to the question
(no "strong fit for the role"), a turn that asks two things gets both answered, a yes-or-no
about the user's own situation is never answered for them, and each role in the digest states
its length, so no answer adds up dates.

**Several sessions at once.** Sessions on separate computers never shared anything but the
account; what went wrong was two copies of the app on one computer, both capturing the same
microphone and speakers. The app now runs once per computer, an interview ends 90 s after the
goodbye goes quiet, and the History tab's "Live now" (#39) ends a stray call from anywhere.

**Checked live** (local, gpt-6-luna and gpt-6.1-sol, the fictional benchmark candidate):

- Detector on `evals/bench/mock_interviews.toml`, 2 repetitions: main's prompt fired on all 6
  screening checks both times (12 false fires, 67% accurate); the branch on none (100%), with
  the other 20 turns unchanged. One type differs from the label I first wrote (the AWS tooling
  question, `technical` both times), now labeled `technical`.
- `copilot-answer-eval` on 6 questions, main against the branch, 1 repetition
  (`evals/answers/results/q1029-before`, `q1029-after`): no invented facts 0.67 to 0.83 on both
  passes, deep completeness 3.67 to 4.17. Asked whether the candidate had worked there before,
  main's fast pass said "no" for them and the branch's asked back what counts. On the two-part
  and the platform questions the detailed answer now follows the cues' order.
- The years question at first came out worse (five years where the profile supports four): the
  benchmark's hand-written digest had no role lengths. With them, as rendered digests now have,
  4 of 4 answers said four (`q1029-years`).

**Not measured live:** the question joining, the end after a goodbye, the mic warning and the
deep hedge were tested with scripted models and the test gateway; the audio test was tested on
synthetic audio, not on a real microphone and speakers.

**Stage 0 cleanup (your OK).** After a backup (`copilot-20261029T210223Z.dump`), profile B's
session lost what came after its call: 29 questions with their answers and 300 transcript lines;
its end and billed length are now its call's (30:35); 21 cached answers written after it ended
were removed from B's exact-repeat cache; and its post-call outputs were rebuilt from what is
left (no word of the other calls remains in them). Nothing was moved to another profile.

**Spend:** 3 detector runs (about 120 calls, about $0.01), 3 answer-eval runs ($0.25), one
post-call rebuild on gpt-6.1-sol (about $0.05): about $0.31. No new infrastructure.

**After merge:** the deploy on merge brings the backend changes. The audio test and the
single-instance guard need a desktop release (0.1.15).
