# Interview, 2026-10-28: a slow fast pass, a resume that lost its HubSpot lines

The first real interview run through the app: session `01a182e8-aa7c-74c5-ab4c-e49039e62aa0`,
started 14:27:28 UTC, 15.6 min (the call itself ran from 04:31 to 13:51 of it), Zoom, desktop
app 0.1.11, against the Stage 0 GCP deployment (backend at `67567fa`, the merge of the
[2026-10-27 fixes](2026-10-27-standup.md); later merges changed only the desktop), OpenAI routes
(gpt-6-luna for the detector, fast pass and summary; gpt-6.1-sol for the deep pass and the
post-call outputs), Deepgram Nova-3 on both channels. Profile: a linked Career-OS profile owned
by the account, with an application pulled from Career-OS 8 minutes before the call (an
integrations engineer role at a HubSpot agency) and its tailored resume. Transcript consent was
on. As before, this document quotes no interviewer words and names no interviewer or company
people: findings are paraphrased. Employer and project names from the user's own resume are
named where speech recognition got them wrong.

## Summary

A recruiter screen: the interviewer described the role, asked seven questions (most recent role,
HubSpot API experience, client work, why a new job, pay, location, questions for them), answered
the user's two questions, and closed. One remote speaker, no follow-up rounds.

What worked: **detection was right on every turn it decided**: 6 of 6 interviewer questions
fired, small talk and the interviewer's long explanations did not, and the one turn it did not
decide (HubSpot) was cancelled by the user's Respond press 0.7 s after the question; replayed
offline it fires 5 of 5 times. **Nothing invented** about the user's work: every cited claim
matches its source, with one mis-citation. The prepared answer for the first question showed at
1.9 s and the user spoke from it.

What went wrong:

- **Three questions in a row got their first words 6 to 8 s late** (07:39, 08:40, 09:52). The
  fast pass on gpt-6-luna got no first token within its 3 s timeout, retried on the same model and
  tier, and timed out again on the third question, which got no fast answer at all; its deep pass
  waited for that failure and showed at 8.1 s. The user had started answering 1.6 to 2.2 s after
  each question. The detector, which has a 2 s hedge to the Priority tier, was not affected.
- **The paired resume's HubSpot and client-facing lines never reached the answers.** The resume
  has two HubSpot bullets (workflows and webhooks at the latest employer, Data Sync at the one
  before), two on working with business teams and clients, and "15 years". Resume extraction kept
  one summary sentence per role, and none of these made it into a role, project or story; the
  HubSpot skills survived only as an uncited list. So the HubSpot answer framed HubSpot as
  something the user would learn ("I'd approach it through its docs"), contradicting both the
  resume and what the user had said a minute earlier, and the client-experience answer had no
  real story.
- **The Respond press threw away work that was about to land.** It cancelled the running detector
  and the held fast pass, started a new pass from scratch (which then hit the timeout above), and
  as a `respond` it may not use a prepared answer, though one matched at 0.72 (threshold 0.63).
- **No keyterms in interview packs**, so speech recognition on the user's mic misheard HubSpot
  three times, the latest employer's name, "REST and SOAP" and the user's state. The model saw a
  garbled product name, which is part of why the HubSpot answer ignored the user's own claim.

Smaller: the motivation question was typed `behavioral` (3 of 3 on replay) and got generic "I'd"
lines instead of the role-and-goals shape; the location question got a fill-in placeholder from
both passes; the pay answer said the band was acceptable, which is the user's call; one harmless
false fire on a one-word check at 13:08.

Latency at the client, question end to first word: 2.4 s p50, 8.1 s p95 (n = 7); deep done
4.8 s p50, 10.2 s p95. Four fast-pass first-token timeouts, one connection error (retried), no
STT drops, no reconnects, one echo line dropped (the goodbye). Cost about $0.12, **about $0.47 per
session hour against the $0.84 estimate** ($0.76 per hour of actual talk).

## 1. Data available

| Data | Where | State |
| --- | --- | --- |
| Session row | `app.sessions` | Present: interview, Zoom, linked to the application, `store_transcript` true, 936 billed s, no interview row or round |
| Transcript | `app.transcript_segments` | 72 "them" segments (4.1 min of speech), 92 "me" (4.5 min) |
| Questions and answers | `app.questions`, `app.answers` | 8 questions (1 forced, as Respond), 14 answers with text, `latency_ms` including `client_first_word_ms`, tokens and cost |
| Detector decisions | Cloud Logging, gateway | All 61 (31 completed, 29 cancelled by newer text, 1 cached; 2 hedged, 0 failed): hash, decision, type, latency; totals with tokens in the session end line |
| Session pack | Valkey `pack:{uid}:*` | **Kept this time**: the session start line names it (`01a182e1-b761-…`, built 14:19:53 UTC, 8 min before the call), 30 prepared answers, 5 ranked stories, 0 keyterms |
| Digest | Valkey, the resume version's | Present: 4 roles, 5 projects, 5 stories, 19 skills, from the tailored resume pulled at 14:19 |
| Resume and JD | `app.resume_versions`, `app.applications` | Present; compared line by line with the digest (section 3) |
| Conversation summaries | Cloud Logging | 2 calls with tokens and stop reason (both `end_turn`). Text gone |
| Held speculative passes | Session end line | A count (42 model passes in all, so about 28 held and thrown away) |
| Post-call outputs | `app.session_outputs` | Summary, per-question feedback, follow-up email, ready 25 s after the end |
| Cloud Logging | `jsonPayload.session_id` | 281 lines: gateway, worker and API |

Missing: the desktop's own events (whether Respond was a deliberate press on the question, what
was on screen when, expand presses beyond the one logged), the held passes' text, and STT billed
audio per stream (estimated from segments).

## 2. Per-question timeline

Times from session start. "User" is when the user's first word came after the question ended,
from the mic transcript. "Shown" is the client's first word of the answer, from the question's
end.

| # | At | Asked (paraphrased) | Detector | Type given | Should be | Shown | User | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 06:00 | Your most recent role, its day to day and the kinds of integrations | Fired, 1.4 s | behavioral | behavioral | Prepared, 1.9 s; deep 3.7 s | 1.3 s (filler), 4.3 s | In time; used |
| 2 | 07:39 | Your experience with the HubSpot API specifically | Cancelled at 0.7 s by Respond (replay: `technical` 5 of 5) | respond (forced) | technical | Fast 7.8 s | 2.2 s | Late; one line used |
| 3 | 08:40 | Your client-facing experience: in which roles, what kinds of clients | Fired early on the first half (08:35), cancelled when the speaker went on; fired again 1.3 s after the end | behavioral | behavioral | Fast 6.4 s; deep 10.1 s | 1.7 s | Late |
| 4 | 09:52 | What is leading you to look for a new job | Fired, 1.3 s | behavioral | other | Fast none (failed); deep 8.1 s | 1.6 s | Late; wrong shape |
| 5 | 10:40 | Is the role's salary band within your expectations; your own range | Wait at 10:36, fired 1.3 s after the end | other | other | Fast 1.9 s; deep 3.8 s | 0.5 s | The user answered first |
| 6 | 10:46 | Where you are located | Fired, 1.1 s | other | other | Fast 1.6 s (a placeholder) | 0.9 s | No use |
| 7 | 10:56 | What questions you have for them | Fired, 1.6 s | other | other | Fast 2.4 s; deep 4.1 s | 2.9 s | In time; used |
| 8 | 13:08 | A one-word check that all was clear, after describing how the team works with clients | Fired, 1.1 s (replay: `none` 2 of 3) | respond | none | Fast 0.9 s | — | False fire, harmless |

Not shown, correctly: two greetings (04:32, 04:49, `small_talk`), the interviewer's offer to
describe the role, the role overview (04:55 to 05:44), the answer to the user's question about
client work (11:34 to 13:02: `none` and `wait` only), the next steps and the goodbye. No false
fire during 3.5 minutes of the interviewer explaining.

Split and merged: question 3 had two parts; the detector fired on the first part, the speaker went
on, the speculative start was cancelled and the full question fired at the end (as designed;
whether the client flickered is not logged). Question 2's forced text began with two stray
acknowledgements: the "them" stream kept one segment open for 89 s (06:00 to 07:29) and another for 38 s
(10:55 to 11:33) across silence. Harmless here.

## 3. Answer quality

**Grounding.** Every personal claim with a citation was checked against its digest entry:

| # | Claims | Matches its source | Notes |
| --- | --- | --- | --- |
| 1 | 3 prepared, 6 deep | 9 of 9 | Role, title and the API-integration story as written |
| 2 | 2 fast, 2 deep | 4 of 4 | But "my concrete experience is CRM-oriented REST APIs" and "I'd approach HubSpot through its documentation" frame HubSpot as new to the user. The resume says otherwise; the digest does not |
| 3 | 1 fast | 0 of 1 | "Gathering business requirements with cross-functional teams" cited the latest role, whose digest entry does not say it. It came from the user's own answer at 06:51 (and is in the resume, but not the digest) |
| 4 | none | — | Reasoning only |
| 5 | none | — | "That range is within what I'm considering" states the user's position on pay, which no data has |
| 6 | none | — | "I'm based in [your city, state]" on both passes; the resume has no location |
| 7, 8 | none | — | Questions back built on what the interviewer said about the team |

**What the digest lost.** The resume version has 26 bullets. The digest keeps one sentence per
role, 5 projects and 5 stories; whatever bullet none of those covers is gone. Gone here: both
HubSpot bullets (proposing HubSpot CRM and Operations Hub workflows with programmable automation
and webhooks; evaluating HubSpot Data Sync and field mappings), both bullets about working with
business teams, product owners and client-facing colleagues on requirements and tradeoffs, and the
summary's 15 years of experience. The HubSpot skills survived as names in an uncited skills line,
and the pack builder wrote "the digest does not establish specific HubSpot implementations" into
the job summary, which every live prompt carries. These were the interview's two main topics.

**Shape.** Glanceable throughout: fast cues 6 to 12 words, 3 or 4 of them; deep answers 3 to 6
lines of 5 to 12 words. Question 1's deep pass gave role, situation and action; there was no
number to give (the resume has none). Question 3 (`behavioral`) got five "I'd" process lines
and no story, since the digest had none on client work. Question 4, typed `behavioral`, got the
"no matching story" fallback: five generic "I'd" lines about what the user would look for,
instead of the `other` shape (the role tied to the user's goals). Question 7 got three
specific questions back, as the shape asks.

**Fast and deep.** They agreed wherever both ran (questions 1, 2, 3, 5, 6, 7); the deep pass
extended rather than contradicted. On question 3 the deep pass dropped the fast pass's one
grounded cue.

**Earlier in the call.** Question 3's fast cue reused the user's own words from question 1, under
the wrong citation. Question 2's answers did not pick up that the user had just claimed HubSpot
and Zoho work: the mic transcript had both as nonsense words. Question 7's questions built on the
interviewer's overview (team size, the senior engineer as the go-to). Two summary calls, 371 and
241 output tokens, neither at the cap.

**What the user said against what was shown.**

| # | User | Why it diverged |
| --- | --- | --- |
| 1 | Spoke from the prepared cues (title, the latest employer, API integrations), then added HubSpot and other CRMs, AI-assisted data work and requirements gathering | Followed; the additions are the user's own |
| 2 | Started at 2.2 s, before anything showed; at 07:58 said the Evolve synchronization-patterns cue nearly word for word, but attributed it to HubSpot work | Late (7.8 s); the cue that was used is the one grounded line |
| 3 | Described client work at a consulting employer, requirements into technical plans, timelines | Late (6.4 s); the one fast cue matches what the user said, the deep pass came at 10.1 s |
| 4 | Generic: new challenges, learning from a team | Late (8.1 s), and the suggestion was generic too |
| 5 | Accepted the band at once, gave no range | The user answered 1.4 s before the cues; the cues' "ask for the target within the band" went unused |
| 6 | Named the state | The app had nothing to give |
| 7 | Asked how engineers engage with clients, close to the first fast cue | Used |

## 4. Latency, errors and cost

At the client, from the question's end, the seven interviewer questions (n = 7, so p95 is the
slowest). Question 2 counts from the question's end, 0.7 s before the Respond press its own
timings start from; question 4's first word is its deep pass.

| Stage | p50 | p95 | Target |
| --- | --- | --- | --- |
| Detector (fired questions) | 1,328 | 1,597 | 0 on the path |
| First word, any pass | 2,396 | 8,055 | 700–1,200 |
| Deep done (gateway) | 4,750 | 10,190 | — |
| User's first word | 1,600 | 2,900 | — |

The app was ahead of the user's first word on 1 of 7 questions (7), and ahead of their first
substantive sentence on 2 (1 and 7). On the four questions without a slow spell (1, 5, 6, 7) the
first word took 1.6 to 2.4 s: the detector (1.1 to 1.6 s) plus release and network.

Fast-pass first token (held from the speech end): 0.8 to 1.3 s on questions 5 to 8; on 2, 3 and 4
the first route gave nothing in 3 s (`openai_first_token_timeout_ms` 3000 on Stage 0), the retry
on the same model and tier took 2.8 to 3.0 s on 2 and 3 and timed out on 4. In the same minutes
the detector's first token took about 1.1 s and the deep pass's 1.2 to 2.5 s. The deep pass
starts only when the fast pass ends, so on question 4 it waited for both timeouts.

Errors: 4 fast-pass first-token timeouts (07:42, 08:42, 09:54, 09:57), one fast pass failed, one
connection error at 13:03 on both a held pass and a detector call (the detector retried in time),
2 detector hedges. No STT drops or reconnects, no refusals. One mic segment dropped as echo (the
goodbye, 100% overlap).

Speech recognition, mic channel (no keyterms were sent): HubSpot misheard 3 times as other words,
the latest employer (DP World) once, Zoho once, "REST and SOAP" once, the user's state once, plus
general noise in the user's speech. The "them" channel got HubSpot and the role terms right.

Cost, this call:

| Item | Basis | Cost |
| --- | --- | --- |
| Answers, logged | 6 fast (1 prepared, free) + 7 deep | $0.041 (deep $0.040) |
| Detector | 61 calls, 49,976 in / 382 out, logged | $0.005 |
| Held fast passes | about 28 discarded × ~6.2k in (70% cached), estimated | ~$0.007 |
| Summaries, prewarm, embeddings | 2 + 1 + 57 calls | ~$0.001 |
| STT | 8.7 min of transcribed speech × $0.0077 (no keyterms) | ~$0.067 |
| Total | | **~$0.12; $0.47 per session hour, $0.76 per talk hour** (estimate: $0.84) |

Rates per session hour: 31 questions (estimate 40), about 235 detector calls (250) and about 110
held passes thrown away (20). The held-pass rate is far lower than in the standups (330 to 400),
since an interviewer mostly asks questions, but still five times the estimate. Post-call outputs
(not live) are not included. The re-runs for this analysis: 11 detector calls, about $0.001.

## 5. Ranked issues

Ranked by effect on the user in a real interview.

### 1. A slow spell stalls the fast pass for 6 s or more

- **Evidence:** questions 2, 3 and 4: first word at 7.8, 6.4 and 8.1 s, against the user starting
  at 1.6 to 2.2 s; 4 first-token timeouts, one fast pass lost entirely (section 4).
- **Root cause:** the fast pass has no hedge. Its first route waits 3 s for a first token, then its
  retry goes to the same model on the same tier, so a slow spell hits both. The detector got a 2 s
  hedge to the Priority tier after 10-27; the fast pass did not. And the deep pass waits for the
  fast pass to end, so it inherits the stall.
- **Fix:** hedge the fast pass like the detector: with no first token after 1.2 s
  (`fast_hedge_ms`), the second route starts alongside the first and the first to answer wins.
  The second OpenAI route runs on Priority (`openai_fast_retry_tier`). When the fast pass fails,
  start the deep pass at once rather than after the last timeout (already true; the time went in
  the timeouts).
- **Expected gain:** questions 2 to 4 would have shown at about 2.5 to 3 s instead of 6 to 8 s.
- **Risk:** a hedge costs one more fast call on the few that need it (about $0.0003 × 1.75).
  Whether Priority is faster in a slow spell is still a claim, as for the detector. Backend only.

### 2. Resume extraction drops the bullets the interview was about

- **Evidence:** both HubSpot bullets, both client-and-requirements bullets and the years of
  experience are in the resume and nowhere in the digest (section 3); questions 2 and 3 answered
  without them, one cue mis-cited, and the pack's job summary told every prompt the digest does
  not establish HubSpot work.
- **Root cause:** the extraction schema gives a role one summary sentence; a bullet survives only
  if a project or story picks it up. The digest renders nothing else.
- **Fix:** extraction keeps each role's highlights: every bullet that no project or story covers,
  shortened to at most 25 words, with platform and tool names as written (and as hedged: "proposed",
  "evaluated" stay). Plus the resume's headline facts (years of experience) on the profile. The
  digest renders a role's highlights under it, under the role's short id, so they can be cited.
  Then, with your OK, re-extract this resume version and rebuild its pack on Stage 0 (one
  extraction on gpt-6-sol and one pack build on gpt-6.1-sol, roughly $0.10 to $0.20).
- **Expected gain:** a HubSpot question gets the user's two HubSpot facts, as hedged as the resume
  has them; a client question gets the requirements-and-tradeoffs bullets as a story to tell.
- **Risk:** a longer digest (about 600 more tokens here, all in the cached prefix). Offline tests
  check the prompt, the schema and the rendering; whether the model keeps every bullet shows on
  the re-extraction. Backend and worker only.

### 3. Pressing Respond during a question throws away the work in flight

- **Evidence:** question 2: the press came 0.7 s after the question; the detector it cancelled
  fires `technical` 5 of 5 on replay, a prepared answer matched at 0.72, and the new pass hit the
  stall: first word 7.8 s.
- **Root cause:** `question.force` resets the turn (cancelling the detector and the held pass)
  and starts over; as `respond`, it never takes a prepared answer.
- **Fix:** a press with no typed text while the turn on the latest utterance is still deciding
  adopts that turn: the held pass is released at once as a forced question (with the prepared
  answer if one matched), and the type is the detector's when it comes, as with a question by
  name. Typed text keeps the old behavior. In an interview, a Respond press on a question also
  takes a prepared answer.
- **Expected gain:** question 2 would have shown the prepared HubSpot answer within about 0.3 s of
  the press. **Risk:** an adopted pass started on an interim text; the release uses what it has.
  Backend only.

### 4. Interview packs send no keyterms, and the mic transcript mangles names

- **Evidence:** 0 keyterms in the session start line; HubSpot misheard 3 times on the mic, the
  latest employer once, plus "REST and SOAP" and the user's state (section 4). The model could not
  tell that the user had claimed HubSpot work.
- **Root cause:** only company (standup and meeting) packs build keyterms.
- **Fix:** interview packs get keyterms too: the hiring company, the user's employers, project
  titles and skill names from the digest (short ones, at most 40, as for company packs).
- **Expected gain:** fewer misheard names on both channels, so later answers and the post-call
  feedback see what the user said. **Risk:** Deepgram bills $0.0013 a minute more with keyterms,
  about $0.06 per talk hour. Backend and worker only.

### 5. Motivation questions are typed `behavioral`

- **Evidence:** question 4, and 3 of 3 on replay; the answer was the generic fallback.
- **Fix:** in the detector prompt, questions about why the user is looking, why this role or
  company, pay, location, availability, notice or the process are `other`, not `behavioral`. Add
  paraphrased cases to the detector tests.
- **Expected gain:** the `other` shape: the role tied to the user's goals. **Risk:** low; checked
  live with a few detector calls.

### 6. A personal detail the data lacks gets a placeholder

- **Evidence:** question 6, "[your city, state]" from both passes ($0.005 of deep pass for it);
  question 5 put the user's acceptance of the pay band in their mouth.
- **Fix:** a prompt rule: never write placeholders or fill-in brackets; a personal detail the data
  does not have (where they live, availability, work authorization, their pay position) is the
  user's to say, so give at most a question back. **Risk:** low.

### 7. The false fire on a one-word check

- **Evidence:** one, harmless, and `none` on 2 of 3 replays. **Fix:** none proposed: tightening
  the `respond` rule for interviews risks missing real checks after the user speaks.

### 8. The cost estimate's held-pass rate

- **Evidence:** about 110 held passes thrown away per session hour here, 330 to 400 in the
  standups, against 20 assumed. **Fix:** add the interview rate next to the standup one in
  ARCHITECTURE.md, "Cost estimate". A doc change.

### Recommended order

1 and 3 first: they decide whether anything is on screen before the user starts talking. Then 2
and 4, which decide whether what shows is the user's own experience (2 needs your OK for the
re-extraction on Stage 0). 5 and 6 are small prompt changes. 8 is a doc change; 7 is no change.
Everything is backend and worker only; no desktop release is needed.

## 6. What this branch changed

You asked for the fixes and better answers on questions like the HubSpot one. Fixes 1 to 6
and 8 are in, each with its own commit and a test that fails on the old code; 7 is not
changed. Backend, worker and one migration (0011, expand only); no desktop release.

| Fix | Commit | Tests |
| --- | --- | --- |
| 1. Fast-pass hedge on Priority | Fast pass: hedge a stalled first token on the Priority tier | `test_the_fast_pass_hedges_on_the_priority_tier` |
| 2. Resume facts reach the answers | Resume facts reach the answers: role highlights, a headline, no gap list; Extraction: projects and stories first, highlights for the rest | `test_resume_highlights_and_headline_reach_the_digest`, `test_extraction_asks_for_highlights_and_a_headline` |
| 3. A forced question takes over the deciding turn | A forced question takes over the turn still deciding | `test_respond_during_a_question_takes_over_its_turn`, `test_respond_in_a_standup_takes_over_but_stays_a_reply` |
| 4. Interview keyterms | Interview packs: keyterms for speech to text | the keyterm assertions in `test_job_description_becomes_a_job_and_a_pack` |
| 5, 6 and answer quality | Answers: specific on a named tool, no placeholders; motivation is 'other' | `test_live_prompts_show_fluency_with_a_named_tool_and_write_no_placeholders`, `test_detector_types_motivation_and_logistics_as_other`, `test_recruiter_screen_script_scores_the_10_28_turns` |
| 8. Cost estimate | ARCHITECTURE.md, "Cost estimate" | — |

The quality work for the HubSpot question is three changes together: the facts (fix 2: the
two HubSpot bullets now reach the digest, as hedged as the resume has them), the pack (no
"the digest does not establish HubSpot" in every prompt; prepared questions on the platforms
the job names), and the live rule (the user's facts on the named tool first, then its
specifics by name, never framed as new to the user). Paraphrased fixtures from this call:
`evals/bench/recruiter_screen.toml` for `copilot-detector-eval`, and two questions in
`evals/answers/questions.toml` (a named platform, location) for `copilot-answer-eval`.

**Checked live** on the Stage 0 VM, branch prompts loaded beside the deployed code, on a
fictional profile shaped like this call (no call text):

- The HubSpot question, old prompts and digest against new: the new fast pass led with the
  resume's HubSpot workflow-and-webhook line, cited, then CRM objects, associations,
  private-app tokens against OAuth scopes, rate limits and webhook signatures; the new deep
  pass added v3 objects, batch upserts, 429 backoff and idempotent webhook handling. The old
  ones kept HubSpot at arm's length ("my core experience: REST integrations"; "I'd distinguish
  integration depth from HubSpot-specific depth").
- Location: no placeholder in 3 of 3 new answers (old: 4 of 4). Pay: neither new answer said
  the band suits the user (old: 1 of 2).
- Detector, new prompt: motivation `other`, the HubSpot question `technical`, location `other`,
  3 of 3 each.
- Extraction, new prompt, on the paired resume itself (printed only, nothing stored): the first
  wording put all 23 bullets into highlights and returned no projects or stories, which is why
  the last commit says projects and stories come first. With it: 4 role summaries, 7 projects,
  4 stories, 13 highlights including both HubSpot bullets and the client bullets, and the
  15-year headline.

**Spend on re-runs:** 11 detector replays (about $0.001), 27 live prompt calls ($0.019), two
extraction calls on gpt-6-sol (3.6k tokens in, 4.7k out: about $0.05 at gpt-6.1-sol prices;
gpt-6-sol has no entry in Stage 0's price table). About $0.07 in all. No new infrastructure.

**After merge (needs your OK):** the deploy on merge applies migration 0011 and the new code,
but this profile's memory and pack still come from the old extraction. Re-extracting the
paired resume version (one gpt-6-sol call) rebuilds its digest and then its pack (one
gpt-6.1-sol call), about $0.10, and replaces that version's 33 memory nodes. Until then, a
new interview for this application still has no HubSpot facts in its answers.

Not measured live: the fast-pass hedge and the forced take-over (tested with scripted models
only; whether Priority is faster in a slow spell shows in the next real call), and the
keyterms' effect on the mic transcript.

## 7. After review: suggestions and following the user

Your call after reading section 6: a resume never holds all of your experience, so interview
answers should suggest close, related details for you to check, and follow you once you start
talking, even when no question was detected. Both are in (ARCHITECTURE.md, "Suggested details"
and Turn detection step 6). Suggested lines end in `[~]`, and the overlay dims them and tags
them "check". Standups and meetings keep the strict rule.

Checked on your real data this time: the paired resume through the new extraction, the real JD
through the new pack prompt, the call's own questions with the transcript up to each, on the
Stage 0 VM (results kept off the repository). The pack now lists the job's requirements and has
HubSpot-specific prepared answers (CRM API, Data Sync, webhooks, programmable automation).
On the HubSpot question the deep pass gives the two resume facts, cited, then hands-on
suggestions marked `[~]` (v3 object endpoints, private-app tokens with scoped permissions),
then batch upserts, 429s, duplicate webhooks and field ownership against sync loops. The
location question gave no placeholder in 2 of 2 runs after the last rule change (one before
it). Follow cards built on what you said (a HubSpot sync claim, consulting client work). Spend
for these runs: about $0.09 (one pack build on gpt-6.1-sol, 26 live passes).
