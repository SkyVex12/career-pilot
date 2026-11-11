# All sessions to 2026-11-10: what limits the answers across calls

Every interview and standup session on Stage 0, 2026-10-21 to 2026-11-10: 21 sessions, 19 with
transcript consent. Ten were analysed one by one before (the documents next to this one);
nine were not (two standups on 10-28, three in November, the 11-10 interview, the 10-22
interview and four short or broken sessions). This document measures all of them the same
way and looks for what repeats. As in the earlier ones, no meeting text is quoted, nobody
else is named, and sessions carry labels instead of ids (I for interview, S for standup).

## Summary

**Grounding is not the problem; timing and turn handling are.** Across 15 sessions with real
calls, cited claims match their source almost every time (section 4), detection finds 91% of
interview questions and 83% of standup turns that are the user's, and nothing has fired for a
teammate since 10-26. What limits usefulness, in order:

1. **The card is late.** The short answer's first word reaches the screen 1.9 s (interviews)
   and 2.0 s (standups) after the question at p50, 3.5 s and 4.8 s at p95. That has not moved
   since late October. The user starts a real answer before the card on 56% of interview
   questions (11 of 14 on the newest call) and 36% of standup turns. The detector is still on
   the path of every card (1.05 s at p50), and in a slow spell its hedge waits a full 2 s:
   11 to 16% of November decisions took 2.5 to 3.2 s.
2. **In a standup, the user's own update gets no card.** When the hand-off is not a question
   (the teammate finishes and stops, or asks whether there is anything to discuss), the
   user gives yesterday and today with nothing on screen or with a card about the previous
   topic: 3 of the 8 standups with a call. The pack's prepared status points have been shown
   once in 9 standups.
3. **Turns come apart.** A question whose first half ended mid-sentence loses that half (2 on
   the newest interview); one teammate turn becomes three cards (11-03); follow cards
   again land on turns already answered, or on the user's own question (5 of 8 follow cards
   in November were wrong, each with a deep pass).
4. **Sessions that start broken stay broken silently.** Two of four November interview
   sessions opened an application more than a day old and ran with no pack (its packs
   expire after 24 hours). One two-hour interview heard nine fragments of the other side and
   one heard none; nothing told the user.
5. **The cue speaks for the user or says nothing.** On the newest interview 5 of 14 cards
   chose between two roles for the user, guessed a work split, asked what an amount
   misheard by speech recognition meant, or gave "explain it briefly" cues. In November
   standups, cards carried the work log's tool bookkeeping (file counts, test counts, review
   reruns), and the user visibly skipped one.

By mode: interviews fail on lateness and on personal questions; standups fail on the missing
update card, card churn inside one turn, and content that reads like a tool report.

By version: the 10-28 and 10-29 fixes held (no lost fast pass since, no screening answers,
deep answers linked to the cues). The 10-30 fixes took the standup's short answer from 2.75 s
to 1.95 s at p50 and its detailed answer from 147 to 48 words, at three times the discarded
fast passes. Regressed: follow cards on answered turns (a new shape), and standup cost per
hour ($0.62 to $0.68 in October, $0.80 to $1.17 in November).

## 1. Inventory

Start times are UTC. "Commit" is the last merge to main before the session started (merges
deploy at once); app versions come from each session's start line. Session ids are in the
hand-over note, not here.

| Label | Mode | Started | Length | App | Commit | Consent | Analysed in |
| --- | --- | --- | --- | --- | --- | --- | --- |
| I0 | interview | 10-21 02:51 | 8.1 min | 0.0.0 | `bb4b93c` (#9) | **off: skipped** | — |
| I1 | interview | 10-22 17:57 | 17.6 min | 0.1.0 | `511da57` (#15) | on | here |
| S1 | standup | 10-26 15:00 | 38.6 min | 0.1.2 | `aa34c38` (#23) | on | 10-26 |
| S2 | standup | 10-27 14:58 | 20.5 min | 0.1.7 | `053d6ff` (#28) | on | 10-27 |
| I2 | interview | 10-28 14:27 | 15.6 min | 0.1.11 | `0fbb382` (#34) | on | 10-28 |
| S3 | standup | 10-28 14:58 | 25.5 min | 0.1.11 | `0fbb382` (#34) | on | here |
| S4 | standup | 10-28 17:10 | 15.5 min | 0.1.11 | `0fbb382` (#34) | on | here |
| I3 | interview | 10-29 15:58 | 53.4 min | 0.1.13 | `bee5ac9` (#37) | on | 10-29 (A1) |
| I4 | interview | 10-29 16:56 | 30.6 min | 0.1.13 | `bee5ac9`, then #38, #39 | on | 10-29 (B) |
| I5 | interview | 10-29 17:29 | 59.2 min | 0.1.13 | `e76182b` (#38), then #39 | on | 10-29 (A2) |
| I6 | interview | 10-29 17:59 | 13.0 min | 0.1.13 | `e76182b` (#38) | on | 10-29 (C) |
| S5 | standup | 10-30 15:00 | 19.0 min | 0.2.0 | `e203313` (#44) | on | 10-30 |
| I7 | interview | 11-02 19:00 | 0.2 min | 0.2.2 | `f493348` (#51) | on | here |
| S6 | standup | 11-03 15:00 | 14.9 min | 0.2.3 | `85d7aba` (#52) | on | here |
| S7a | standup | 11-04 14:59 | 0.1 min | 0.2.3 | `85d7aba` (#52) | on | here (a restart) |
| S7 | standup | 11-04 14:59 | 8.6 min | 0.2.3 | `85d7aba` (#52) | on | here |
| S8 | standup | 11-05 14:59 | 22.5 min | 0.2.3 | `5e94475` (#53) | on | here |
| I8 | interview | 11-05 16:57 | 119.9 min | 0.2.4 | `5e94475` (#53) | on | here |
| S9 | standup | 11-09 15:00 | 5.2 min | 0.2.4 | `5e94475` (#53) | on | here (no call) |
| I9 | interview | 11-09 16:49 | 4.6 min | 0.2.4 | `5e94475`, then #54 | on | here |
| I10 | interview | 11-10 17:28 | 16.8 min | 0.2.8 | `d552c5a` (#57) | on | here |

Skipped: I0 (consent off; 9 questions, no text stored). One meeting on 10-22 also has
consent off and is not an interview or standup. I4 is its own call only: the part that
answered other calls was removed on 10-29. S9 had no call (the teammate was away); the user
typed two questions, and both got grounded answers.

**Which fixes were live.** Each analysis's fixes went live with its merge:

| Fixes (merged, UTC) | Live from | Not yet live for |
| --- | --- | --- |
| 10-26 standup: group rule, `respond`, Ask-box commands, work-log retry, short prepared points, post-call date, keyterms (#26, 10-26 22:30) | S2 | I1, S1 |
| 10-27 standup: turn-taking block, question by name, commitments merged and closed, summary cap, pack and detector totals logged (#31, 10-28 01:31) | I2, S3 | I1, S1, S2 |
| 10-28 interview: fast hedge, resume highlights, forced take-over, interview keyterms, motivation typed `other`, no placeholders, suggested details, follow the user (#35, 10-29 04:27) | I3 | all of 10-28 and before |
| 10-29 interviews: follow only on unanswered turns, deep answer develops the cues, screening checks `none`, question in pieces, deep hedge, end after goodbye, mic warning (#41, 10-29 22:41) | S5 | all of 10-29 and before |
| 10-30 standup: held pass for every teammate sentence, early decision, short standup deep answer, STT nudge, backchannel, cue wording (#48, 10-30 16:38) | I7, S6 | S5 and before |

## 2. How this was measured

- **From the stage, read only:** session rows, decrypted transcripts, questions and answers
  with their `latency_ms`, post-call outputs, the company's memory nodes, work-log dates,
  digests and packs still in Valkey, and each session's Cloud Logging lines (20 to 2,354
  per session). Everything stayed in a scratch directory outside the repository.
- **Counted by script for every session:** latency at the client (`client_first_word_ms`),
  deep-pass end, words per answer, citations and whether each resolves to an entry,
  detector totals, held passes, errors by kind, echo, cost. "In time" is: the short answer's
  first word was on screen before the user's first line of four or more words after the
  question.
- **Labelled by hand, by reading the timeline:** which turns were for the user, false
  fires, splits, types, and what the user said against what was shown. Done in this pass
  for I10, S3, S4, S6, S7, S8 and the four broken sessions; I1's fired cards only. For the
  ten sessions analysed before, the counts are those documents'.
- **Limits.** Hand labels are one reader's judgement. Usefulness is inferred from the mic
  transcript, which is poor on the user's speech (section 7). The desktop still sends no
  events of its own (what was on screen, expand presses), and no wake time before a call.

## 3. Detection

Per session. "For me" counts the other side's turns that needed more than a yes or no from
the user. "False" is a card for something not put to the user, or put mid-explanation.

| Session | App | For me: fired / all | Missed | False | Split or merged | Type wrong | In time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| I1 | 0.1.0 | 9 fired (misses not labelled) | — | 0 | 0 | 3 of 9 | no mic text |
| I2 | 0.1.11 | 6 / 6 (+1 forced) | 0 | 1 | 1 | 1 of 7 | 1 of 7 |
| I3 | 0.1.13 | 15 / 18 (3 forced) | 3 | 1 | 6 questions, 15 cards | 1 of 25 | no mic text |
| I4 | 0.1.13 | 13 / 14 | 1 | 1 | 1 | 0 | 9 of 15 |
| I5 | 0.1.13 | 1 / 1, then not English | — | 0 | 0 | 0 | no mic text |
| I6 | 0.1.13 | 20 / 21 | 1 | 0 | 1 | 1 of 21 | 12 of 21 |
| I10 | 0.2.8 | 14 / 16 | 2 | 0 (2 needed no card) | 3 | 0 | 3 of 14 |
| **Interviews** | | **69 / 76 (91%)** | 7 | 3 | 12 questions | 6 of 92 | **25 of 57 (44%)** |
| S1 | 0.1.2 | 2 / 3 | 1 | 5 | 0 | 0 | 2 of 2 |
| S2 | 0.1.7 | 3 / 8 | 5 | 0 | 0 | 0 | 1 of 2 |
| S3 | 0.1.11 | 7 / 7 | 0 | 0 | 0 | 1 (the hand-off) | 4 of 7 |
| S4 | 0.1.11 | 16 / 16 | 0 | 0 | 0 | 2 | 8 of 14 |
| S5 | 0.2.0 | 8 / 8 | 0 | 0 | 0 (2 duplicate follows) | 0 | 5 of 8 |
| S6 | 0.2.3 | 6 / 8 | 2 | 2 (one turn in pieces) | 1 turn, 3 cards | 2 | 6 of 7 |
| S7 | 0.2.3 | 4 / 5 | 1 | 0 | 0 | 0 | 2 of 4 |
| S8 | 0.2.3 | 4 / 5 | 1 | 2 | 0 | 0 | 2 of 3 |
| **Standups** | | **50 / 60 (83%)** | 10 | 9 (4 since S1) | 1 | 5 of 59 | **30 of 47 (64%)** |

**Interviews, across versions.** Recall has been 87 to 100% since 10-28 and is not the
limit. What repeats:

- **Late for short questions.** The user answers factual questions (availability, current
  job, a language, a database) within a second. On I10 the card came first on 3 of 14.
- **Pieces.** Before the 10-29 fix a paused question made several cards. After it, the
  cards join, but a first half the detector judged unfinished is dropped when the speaker
  goes on after a pause: on I10 two cards carried only the question's last words (the
  answers were still on topic, from the transcript block). One question was decided twice.
- **Questions that need no card.** Screening checks are quiet since 10-29 (work
  authorization on I10 showed nothing, as designed). Two of the same kind still fired on
  I10: whether the user is working now, and whether a pay figure on file is right.
- **Missed on a trailing-off question.** The one real miss on I10 ended mid-sentence; the
  user answered after 3 s and a follow card came at 11 s.

**Standups, across versions.** The 10-26 failure (cards for teammates) has not returned in
7 standups. The 10-27 failure (turns for the user missed) went from 5 of 8 to 4 of 18 on
0.2.3. What repeats now:

- **The update turn.** On S6 and S7 the teammate finished their own update and stopped;
  the user began theirs with nothing on screen. On S3 the teammate asked whether there was
  anything to discuss; a `respond` card about the previous topic showed while the user
  gave yesterday and today. Follow cards came 4 to 9 s into the update and were about the
  teammate's topic.
- **Cards inside one turn.** With a held pass for every sentence (10-30), a card can fire on
  a statement in the middle of an explanation: 4 in November (S6: one turn gave three
  cards in 21 s, each replacing the last and restarting its deep pass; S8: one on a
  rhetorical aside while sharing a screen, one on advice mid-explanation).
- **Checks that need a yes.** On S4, a technical one-on-one run in standup mode, 7 of 16
  cards were for "are you following?" checks. The cards were right and unnecessary.

**Follow cards** (both modes, since 10-29's rule "only where nothing answered"):

| Session | Follow cards | Right | On an answered turn | On the user's own question |
| --- | --- | --- | --- | --- |
| S5 (before the backchannel fix) | 6 | 4 | 2 duplicates | 0 |
| S6 | 3 | 1 (late, on the update) | 1 | 0 |
| S7 | 1 | 0 | 0 | 1 |
| I10 | 4 | 1 | 2 | 1 |

The answered-turn check looks for a question at or after the other side's last line. A
trailing fragment or an audio check after the question moves that line, so the turn looks
unanswered. And a user who asks something (a request to repeat, a date) is "following"
by word count.

## 4. Answers

**Grounding.** Every citation in every session resolves to an entry (437 of 438; the one
left is a slip of a letter in the id). Claim by claim:

| Sessions | Cited claims checked | Wrong | Personal claims with no source, unmarked |
| --- | --- | --- | --- |
| I2 (10-28 doc) | 14 | 1 mis-citation | 2 (pay position, a placeholder) |
| I4, I6 (10-29 doc) | 133 | 2 (an employer, a sum of years) | 4 (screening answers) |
| I10 (97 cited lines; the 14 weakest by word overlap read by hand) | 97 | 1 (a framework the user had just said, cited to a project) | 1 (which of two roles the user prefers) |
| S6, S7 (cited lines under 0.6 overlap read by hand) | 45 | 0 | 0 |

The invented-claim rate is under 2% of cited claims in every batch and has not grown. The
interview policy of suggested details marked `[~]` works as built: on I10 the user read a
detailed answer aloud including two marked lines, and attached the project to the wrong
employer, which the post-call feedback caught.

**Shape and length.**

| Mode, app | Short answer, words | Detailed answer, words | Style |
| --- | --- | --- | --- |
| Interview 0.1.0 to 0.1.11 | 33 to 34 | 36 to 38 | bullets |
| Interview 0.1.13 | 34 to 40 | 108 to 157 | paragraph |
| Interview 0.2.8 | 37 | 128 | paragraph |
| Standup to 0.1.11 | 38 to 56 | 23 to 57 | bullets |
| Standup 0.2.0 | 42 | 147 | paragraph |
| Standup 0.2.3 | 39 to 42 | 44 to 52 | short paragraph (10-30 fix) |

Short answers are steady at 3 or 4 cues. Shapes fit the type where the type was right.
Cues that fit any question still appear when the data has nothing: on I10, asked why a
role ended, the cues were to keep it brief and to ask what the other side wanted to know;
on I1 (before the rule) four cards were of this kind.

**Fast and deep.** Since the 10-29 change (the detailed answer develops the cues) they
agree: no contradiction on I10, S6, S7 or S8. Before it: 1 on I3, 1 on I6, 1 on S5, 1 on S3
(the short answer said fixes were merged; the detailed one said production was not
confirmed).

**Earlier context.** Used well where the mic transcript is readable: on I10 later answers
took up the switch to another role made earlier in the call; on S6 and S7 answers carried
what the user had promised minutes before. Missed once on S4: asked the name of a library
the previous card had named, the short answer said the name was not clear.

**Speech recognition changes the answer.** Three times a misheard word led the card
astray: a yearly pay figure written as a small dollar amount (I10; the card asked what it
meant), a contract type written as a number (I1; same), and "with" heard as "without" (S8;
the card advised the opposite of what was asked).

## 5. Usefulness

What the user said against what was shown, for the sessions labelled by hand in this pass
(55 cards), with the 10-28 and 10-29 documents' findings beside them.

| | Spoke from it or matched it | A yes or okay was enough | Ignored |
| --- | --- | --- | --- |
| I10 (15 cards with the one useful follow) | 7 (4 nearly word for word, 3 adapted) | 0 | 8 |
| S3, S4, S6, S7, S8 (41 cards) | 9 | 15 | 17 |

Why ignored:

| Reason | I10 | Standups |
| --- | --- | --- |
| Late: the user had answered | 3 | 2 |
| Wrong: chose for the user, or misled by a misheard word | 2 | 6 |
| Generic or handed the question back | 1 | 1 |
| The user had their own story or number | 2 | 0 |
| A duplicate or a piece of a turn already carded | 0 | 7 |
| Too detailed to say (tool bookkeeping) | 0 | 1 |

Earlier batches read the same way: on I2 the user spoke from 2 of 7 cards and 4 were late;
on I6 the user read 8 detailed answers nearly word for word; on I4 the user's own words
mostly matched the first cues.

Two patterns:

- **When the card is in time and about the user's work, it is used.** Experience
  questions in interviews (a summary, a project, leadership) and status questions about a
  named item in standups were spoken from in 13 of 17 cases, by my count.
- **In standups a third of the cards answer a check that a word settles.** They cost a
  short and a detailed answer each and move the panel while the user listens.

## 6. Standups

**Yesterday, today, blockers for the date.** The pack takes "yesterday" from the last log
day before the standup. A log for the previous working day existed for 7 of 8 standups
(S1, a Monday, had Thursday's; since the 10-26 fix the pack names the day). Packs were
built 31 to 48 minutes before S6, S7 and S8. But the sections were rarely shown:
prepared status points hit once in 9 standups (best similarity since: 0.09 to 0.45 against
the 0.63 threshold), because nobody asks for the update in the prepared questions' words;
the hand-off is a pause or an open invitation (section 3).

**Work-log and pack content.** 10 logs, 158 active work items (22 words on average), 66
decisions, no log stuck since the 10-26 fix. Items are accurate and specific. 20 of the
158 (13%) carry the coding tool's bookkeeping: files not yet committed, test counts,
review reruns, an internal defect described in security terms. Cards repeated those to a
tester on S6 (3 cards) and S7 (2); on S7, asked what was left, the card listed three
internal defects and the user said only that minor things remained.

**Post-call.** Reports were ready 6 to 45 s after the call where that was logged. Yesterday and today match
what was said in all 8. Commitments: 21 stored, 14 closed by later logs or standups, 7
open. Dates work when one is said (3 of the 6 November commitments have one). Five of the
7 open ones are from 10-27 and 10-28, have no date, and nothing will close them: they stay
in every pack. When the user gives no update (S4, S8), "Yesterday" is empty although the
pack has it.

## 7. Latency, reliability, cost

**Latency at the client**, question end to first word of the short answer, and to the end
of the detailed answer (detected questions; forced and follow cards left out; p95 is
nearest rank).

| Mode | App | Questions | Short p50 | Short p95 | Detailed done p50 | p95 | Detector p50 / p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Interview | 0.1.0 | 9 | 1.59 s | 1.81 s | 3.40 s | 3.99 s | 0.93 / 1.57 s |
| Interview | 0.1.11 | 7 | 1.91 s | 8.06 s | 4.75 s | 10.19 s | 1.16 / 2.59 s |
| Interview | 0.1.13 | 62 | 1.86 s | 2.66 s | 8.61 s | 15.74 s | 1.06 / 2.11 s |
| Interview | 0.2.8 | 14 | 2.01 s | 5.29 s | 7.54 s | 8.88 s | 1.28 / 2.97 s |
| **Interview** | all | 92 | **1.87 s** | **3.52 s** | 8.17 s | 15.38 s | 1.07 / 2.34 s |
| Standup | 0.1.2 | 7 | 1.68 s | 2.21 s | 5.69 s | 8.23 s | 1.04 / 1.95 s |
| Standup | 0.1.7 | 3 | 3.27 s | 3.52 s | 6.98 s | — | 1.42 / 5.00 s |
| Standup | 0.1.11 | 23 | 2.05 s | 2.98 s | 5.04 s | 6.01 s | 0.94 / 1.64 s |
| Standup | 0.2.0 | 8 | 2.75 s | 6.16 s | 8.26 s | 13.10 s | 0.95 / 1.46 s |
| Standup | 0.2.3 | 18 | 1.95 s | 3.79 s | 4.73 s | 5.85 s | 1.12 / 2.74 s |
| **Standup** | all | 59 | **1.95 s** | **4.83 s** | 5.28 s | 9.65 s | 1.05 / 2.68 s |

- The target is 0.7 to 1.2 s. No version has met it; the floor is the detector call.
- The 10-30 fixes helped standups: 2.75 s to 1.95 s at p50, detailed answer done in 4.7 s
  instead of 8.3 s. The early decision was used on 4 of the 18 questions and saved 38 to
  407 ms on those; the held pass for statements did most of the work.
- The slow tail is the provider plus the hedge. On S8, 55 of 208 detector calls hedged,
  and 48 of 438 decisions across S6 to S8 took 2.5 to 3.2 s: the 2 s wait before the second
  route, then its own 0.8 s. I10: 9 of 55. The fast pass hedges at 1.2 s; the detector
  still waits 2 s.
- The 0.1.11 interview's 8 s tail (three stalled fast passes) has not recurred since the
  fast hedge: no fast pass was lost in any later session.

**Reliability.**

| | Count | Sessions |
| --- | --- | --- |
| Short answers lost | 1 | I2 (before the hedge) |
| Detailed answers lost | 2 | I4 |
| Detector decisions failed | 12 | S1 1, S2 10, I3 1; none since 10-29 |
| Detector hedges | 85 since the totals were logged (10-28) | S8 55, I10 12, the rest 0 to 5 each; S2 had 29 |
| Fast or deep first-token failures | 28 | I4 13 deep, I2 5 fast; 1 or 2 elsewhere |
| STT stalls or reconnects | 12 | S5 6, I0 3, I4 2, I3 1; **none since the 10-30 fix** (1 nudge on I10, answered) |
| Memory search over its 100 ms | 20 | every standup but S4 (1 to 6 each) |
| Gateway restarts under a call | 5 | I4 4, I5 1 (the 10-29 deploys); none since |
| Mic lines dropped as echo | 21 | 1 to 3 per call, nearly all the goodbye |
| Summaries at the cap | 6 | I3 2, I4 3, S5 1; none since |
| Started without a pack | 3 | I3 (the 10-29 bug, fixed), **I7, I8** (pack expired) |
| Little or no audio from the other side | 2 | **I8** (9 fragments in two hours), **I9** (none in 4.6 minutes) |

- **Expired packs.** An application's pack lives 24 hours unless an interview date is set.
  I7 and I8 opened applications from 10-29: digest loaded, no pack, so no job summary, no
  prepared answers, no keyterms. The gateway asked for a rebuild; the running session never
  takes it.
- **No audio from the call.** On I8 and I9 the user's mic came through and the call's
  audio did not (I8: a few words; the user asked several times whether they could be
  heard). Both are app 0.2.4 on the same profile; the three standups on 0.2.3 and I10 on
  0.2.8 were fine. The server cannot tell a capture fault from a quiet call, and the app
  logs nothing about its devices. The mic has a "we can't hear you" warning; the call side
  has none. I8 also stayed live for 80 minutes after its last recognized word: the idle
  end counts speech flags, not words.
- **Speech recognition on names and terms.** Company keyterms have been on since 10-27
  (40 per pack) and the other side's product and ticket names come through. The user's own
  channel is much worse: framework and cloud service names, a database, the user's
  employer and "code reviews" were garbled on I10; a teammate's name was written three
  ways on S6. Amounts are the costly case (section 4).
- **VM wake time** is not recorded per call. Today the instance took 25 s to start; the
  API was last measured up 54 s after a start (11-02).

**Cost** (answers as logged; detector from its logged tokens; discarded held passes at
$0.0002; speech to text from transcribed minutes at $0.0090 plus 10%).

| Session | Answers | Detector | Held passes | STT | Total | Per live hour |
| --- | --- | --- | --- | --- | --- | --- |
| I1 | $0.02 | $0.01 | $0.01 | $0.07 | $0.10 | $0.35 |
| I2 | $0.04 | $0.01 | $0.01 | $0.09 | $0.14 | $0.56 |
| I3 | $0.14 | $0.04 | $0.02 | $0.15 | $0.34 | $0.38 |
| I4 | $0.21 | $0.05 | not logged | $0.24 | $0.49 | $0.96 |
| I6 | $0.19 | $0.01 | $0.01 | $0.11 | $0.30 | $1.40 |
| I10 | $0.13 | $0.01 | $0.01 | $0.13 | $0.28 | $1.00 |
| S1 | $0.07 | $0.04 | $0.05 | $0.27 | $0.44 | $0.68 |
| S2 | $0.02 | $0.02 | $0.02 | $0.16 | $0.22 | $0.65 |
| S3 | $0.02 | $0.03 | $0.01 | $0.12 | $0.18 | $0.42 |
| S4 | $0.06 | $0.03 | $0.02 | $0.10 | $0.21 | $0.82 |
| S5 | $0.12 | $0.04 | $0.02 | $0.17 | $0.35 | $1.11 |
| S6 | $0.08 | $0.04 | $0.05 | $0.13 | $0.29 | $1.17 |
| S7 | $0.04 | $0.01 | $0.02 | $0.05 | $0.11 | $0.80 |
| S8 | $0.05 | $0.05 | $0.07 | $0.15 | $0.31 | $0.83 |

- **Interviews: about $0.67 per live hour** over the six calls above; **standups: about
  $0.77** over eight. The estimate is $0.84. All 19 sessions cost about $3.80.
- Dense calls run over it: I6 at $1.40 (129 cards an hour) and I10 at $1.00 (64 an hour),
  the detailed answer being 46 to 63% of a session's cost.
- Standups cost more since 10-30: discarded held passes went from about 330 an hour to
  about 915, detector calls from about 900 to about 1,000. On gpt-6-luna that is $0.05 to
  $0.07 a call. The 10-30 warning stands: on a Claude fast route it would be ten times that.
- I8 billed two hours of session time for nine fragments.

## 8. Did earlier fixes hold?

| Fix | From | Held? |
| --- | --- | --- |
| No cards for teammates | 10-26 | **Yes**: 0 in 7 standups |
| Question by name within a second | 10-27 | Yes (S6: fired on a turn that named the user) |
| Turns for the user in a small standup | 10-27 | Mostly: 14 of 18 on 0.2.3; the update hand-off is the gap |
| Work log stuck after an outage | 10-26 | Yes: all 10 logs extracted |
| Short prepared standup points | 10-26 | Not exercised: no prepared hit since |
| Post-call date and clean commitments | 10-26 | Yes; undated old commitments linger |
| Commitments merged and closed | 10-27 | Yes: 1 to 3 per call, 14 closed |
| Summary cap | 10-27 | Yes: none at the cap since 10-30 |
| Fast-pass hedge | 10-28 | **Yes**: no short answer lost since |
| Resume highlights in the digest | 10-28 | Yes: I10's answers cite them |
| Interview keyterms | 10-28 | Yes (40 per pack), when there is a pack |
| Motivation typed `other`, no placeholders | 10-28 | Yes on I10 |
| Screening checks show nothing | 10-29 | Yes for the listed kinds; two unlisted kinds fired on I10 |
| Follow only on unanswered turns | 10-29 | **Partly regressed**: 3 of 8 November follow cards were on answered turns |
| Question in pieces answered whole | 10-29 | Partly: joined after a card, lost after a "wait" |
| Detailed answer develops the cues | 10-29 | **Yes**: no contradiction since |
| Deep-pass hedge | 10-29 | Yes: one detailed answer at 14.8 s since, on a follow card |
| End after the goodbye; one app per computer | 10-29 | No runaway session since; I8's idle hour is a different cause |
| Held pass for statements, early decision | 10-30 | Yes: 2.75 s to 1.95 s; costs three times the held passes |
| Short standup detailed answer | 10-30 | **Yes**: 147 to 48 words |
| STT nudge before a reconnect | 10-30 | **Yes**: no stall since |
| No duplicate follow after a "mhmm" | 10-30 | Yes |
| Cues never hand the question back | 10-30 | Partly: 2 on I10, 1 on S7 |

Regressed or new since those fixes: follow cards on answered turns; cards in the middle of
a teammate's explanation (a side effect of holding a pass for every sentence); standup
cost per hour; sessions without a pack.

## 9. Ranked issues

Ranked by how often the user ends up without help that the system could have given.

### 1. The card is late, most of all in slow spells

- **Evidence:** short answer 1.87 s (interviews) and 1.95 s (standups) at p50 over 151
  questions; in time on 44% and 64%; I10: 3 of 14. Detector 1.05 s at p50 on every card;
  48 of 438 November standup decisions and 9 of 55 on I10 took 2.5 to 3.2 s (the 2 s hedge
  wait plus the second route).
- **Hit:** all 15 sessions with a call, both modes.
- **Fix (a), small:** hedge the detector at 1.2 s instead of 2 s, as the fast pass does.
  **Impact:** about 0.8 s off the 11 to 16% of cards that fall in a slow spell; p95 from
  about 3.8 to 5.3 s toward 3 s. Costs one more detector call on those (about $0.0002).
- **Fix (b), your decision (you chose it: section 11):** in interviews, show the held
  short answer before the detector decides when the utterance is plainly a question (ends
  in a question mark from speech recognition, opens with a question word or "tell me",
  eight words or more), and withdraw it if the detector says no. **Impact:** about 1 s off
  the clearest questions, which would put them near 1 s. **Risk:** a card that appears and
  is withdrawn, which you ruled out on 10-14. The eval set's interview labels put a number
  on it before any change: see section 10.

### 2. In a standup, the user's own update has no card

- **Evidence:** S3 05:56 (an invitation to discuss; a card on the previous topic), S6 04:39
  and S7 02:32 (the user starts the update after the teammate stops; nothing shown, follow
  cards at 5 to 9 s on the teammate's topic). Prepared status points: 1 hit in 9 standups.
- **Hit:** 3 of 8 standups with a call. Standups only.
- **Fix:** when the user opens their update in a standup with nothing on screen ("for my
  update", "from my side", "yesterday I"), show the pack's prepared update at once, from
  the mic's live text, with no model call; the short detailed answer follows as for any
  status card. And tell the detector that an open hand-off to the user in a standup
  ("anything from your side?") is `standup_status`, answered with the update.
- **Impact:** a card with yesterday, today and blockers within about a second of the
  user's first words, on the one turn every standup has. **Effort:** small (gateway and
  one prompt line); backend only.

### 3. A question's first half is dropped after a pause; one turn makes several cards

- **Evidence:** I10 03:51 and 09:44 (cards whose question was only the last words; the
  first half had been judged unfinished and the speaker went on after a pause); I10 07:07
  and 07:10 (decided twice); S6 11:49, 12:00, 12:10 (three cards and three detailed
  answers for one turn).
- **Hit:** I10, S6; the same family as I3's 15 cards for 6 questions.
- **Fix:** a turn judged unfinished keeps its words: when the same speaker goes on within
  the continuation gap and the user has not spoken, the next decision is made on both
  parts. **Impact:** whole questions on the card and in the post-call record; one fewer
  detector call per such turn. **Effort:** small, in `turn.py`. The three-card turn is
  left as is: each card was a real addition, and holding cards back would cost more
  than it saves.

### 4. Follow cards on answered turns and on the user's own questions

- **Evidence:** I10 04:02 and 06:21 (a trailing fragment and an audio check after a
  question that had its card), S6 12:43, I10 03:41 and S7 04:53 (the user was asking, not
  answering). 5 of 8 November follow cards; about $0.005 each.
- **Hit:** I10, S6, S7; both modes.
- **Fix:** a turn counts as answered if any question fired since the user last spoke and
  within 15 s before the other side's last line; no follow when the user's first sentences
  are a question. **Impact:** the duplicates and the two wrong cards gone; the useful
  follows (a missed question, 3 in these sessions) unchanged. **Effort:** small.

### 5. Sessions that start broken give no sign

- **Evidence:** I7 and I8 with no pack (applications older than a day); I8 and I9 with
  little or no audio from the call; I8 live for two hours.
- **Hit:** 3 of the 4 November interview sessions before I10. Interviews.
- **Fix:** (a) an application's pack lives 30 days, not 24 hours (it is rebuilt on every
  change anyway, and the gateway already refuses one from another resume version); (b)
  when the user's mic has two minutes of speech and the call side none at all, the badge
  says the call's audio is not coming through, as it does for the mic. **Impact:** an
  interview on an older application has its job summary, prepared answers and keyterms;
  a dead call channel is visible in two minutes, not after the call. **Effort:** small;
  backend and worker. Why 0.2.4 captured nothing cannot be told from the server: it needs
  the desktop to report its capture devices and levels (a desktop change, not proposed
  here).

### 6. Cards that speak for the user, or say nothing

- **Evidence:** I10 05:20 (chose one of two roles; the user chose the other), 06:34 (why
  a role ended: "keep it brief" and a question back), 10:57 (a guessed work split, marked
  as a suggestion), 15:14 (asked what a misheard amount meant), 05:58 and 15:14 (yes-or-no
  about the user's own situation); S7 05:56 (asked the teammate their own question back);
  I1 09:06 (a misheard contract type).
- **Hit:** I1, I10, S7: 7 cards. Mostly interviews.
- **Fix:** prompt rules: a choice that is the user's (between roles, arrangements, dates)
  gets what to weigh, never the choice; a split or a share of their time is theirs to say;
  an amount that speech recognition wrote oddly is read as the amount that makes sense,
  never asked about. Detector: whether the user is employed now, and "is that right?" about
  a figure on file, are `none`. **Impact:** about a third of I10's ignored cards. **Effort:**
  small; measured on the eval's answer checks.

### 7. Standup cards repeat the work log's tool bookkeeping

- **Evidence:** S6 (3 cards) and S7 (2): files not committed, test counts, review reruns,
  internal defects in security terms; the user skipped the S7 one. 20 of 158 work items.
- **Hit:** S6, S7. Standups.
- **Fix:** a standup rule: say progress as to a teammate (what works, what is left, when);
  leave out commit state, file and test counts and review bookkeeping unless asked. The
  same line in the work-log extraction prompt for new logs. **Impact:** cards the user can
  say as written. **Effort:** small.

### 8. Smaller, no change proposed now

- **Standup cost per hour** rose with the held pass for every sentence. It bought 0.8 s.
  Keep; revisit before any Claude fast route.
- **Checks that need a yes** get full cards (15 of 41 standup cards). Making them `none`
  would undo part of the 10-27 fix; a lighter card needs a design decision.
- **Old commitments without a date** stay open (5 of 7). A clean-up needs your OK, as on
  10-27.
- **The user's speech is poorly recognized.** Keyterms are already sent; the rest is the
  microphone and the provider.
- **Wake time and desktop events** are still not recorded.

### Recommended set

1(a), 2, 3, 4, 5 and 6, with 7 if you want standup wording changed: all backend, worker and
prompts; no desktop release needed for them (merging this branch still cuts one, as every
merge does). 1(b) is the only change that could reach the 1 s target and is yours to
decide.

## 10. Eval set and baseline

`evals/sessions/` holds three paraphrased scripts of these calls (a recruiter screen, a
two-person standup, a three-person standup: 81 lines with times, addressee and type), and
`copilot-session-eval` scores them in three parts (evals/README.md, "Session eval"):
`flow` replays a script through a real gateway offline, with the detector answering from the
labels, so it measures turn handling alone; `detect` and `answers` call the configured
models.

Baseline on main (`d552c5a`) and the branch, 2 repetitions of the live parts, gpt-6-luna and
gpt-6.1-sol (`evals/sessions/results/baseline/` and `after/`):

| | Baseline | After |
| --- | --- | --- |
| Flow: one card per question | 14/14 | 14/14 |
| Flow: no card where none is due | 26/26 | 26/26 |
| Flow: the whole question on the card | **0/2** | **2/2** |
| Flow: follow cards right | **1/5** | **5/5** |
| Flow: update card in a standup | **0/1** | **1/1** |
| Flow: cards shown, then withdrawn | 0 | 0 |
| Detect: questions for me fired | 28/28 | 28/28 |
| Detect: false fires | 5/54 | 4/54 |
| Detect: type right | 19/26 | 23/26 |
| Answers: checks passed, short answer | 38/44 | 42/44 |
| Answers: checks passed, detailed answer | 34/44 | 44/44 |

On main the offline part fails where the real calls did: both questions with a first half
before a pause lose it (issue 3), four of five follow cases are wrong (issue 4), and the
update turn gets no card (issue 2). On the branch all 48 checks pass.

Live, read with care: two repetitions, and the model's answers vary run to run (an earlier
baseline run scored 27/28 fired and 79/88 checks; an earlier branch run missed one standup
question twice, which led to rewording the hand-off rule; three more repetitions then fired
42 of 42 with 7 of 81 false). What moved for a reason: the open hand-off is typed
`standup_status`; the standup card no longer repeats the work log's bookkeeping (0 of 16
failed checks, from 8); the remaining failed answer check is the misheard pay figure, which
the short answer still asks about in both repetitions, and the detector still fires on it in
one. The statement in the middle of a teammate's turn still fires. Detector latency was
1.3 s at p50 in the baseline run and 1.5 to 2.0 s in the branch runs an hour later: the
prompt grew by about 60 tokens, which does not explain it; the provider's latency drifts by
this much (2026-10-14 analysis).

What the eval cannot show: the time saved by the earlier hedge and by showing a plain
question before the detector. Both need the provider's real timing; the unit tests check
the behavior with a slow scripted detector, and the next real interview's `first_shown`
count and `client_first_word_ms` will show the effect.

**Spend:** six eval runs (two baselines, three on the branch, one detector-only), about 800
detector calls and 260 answers: about $0.26 of the $2 limit. No speech to text, no new
infrastructure.

## 11. What this branch changed

You chose every fix, with speed and quality first and cost kept down. Each has a test that
fails on main. Backend, worker and prompts only.

| Issue | Change | Setting | Tests |
| --- | --- | --- | --- |
| 1(a) Late in slow spells | The detector hedges at 1.2 s instead of 2 s | `detector_hedge_ms` | `test_the_detector_hedges_as_early_as_the_fast_pass` |
| 1(b) Late on every card | Interviews: a plain question shows as soon as its final is in, before the detector decides; the detector types it or the card is withdrawn | `first_show_min_words` (8; empty: off) | `test_a_plain_interview_question_shows_before_the_detector_decides`, `test_a_card_shown_first_is_withdrawn_when_the_detector_says_no`, `test_nothing_shows_before_the_detector_in_a_standup_or_when_turned_off`, `test_a_plain_question_is_told_from_one_the_detector_must_decide` |
| 2 The update turn | Standups: the pack's prepared update shows when the user opens their update with nothing on screen; an open hand-off is typed `standup_status` and answered with the update | — | `test_a_standup_update_gets_the_prepared_card_as_the_user_starts`, `test_no_update_card_in_an_interview_or_over_a_card_already_shown`, `test_a_standup_is_told_about_the_hand_off_and_to_leave_out_tool_bookkeeping` |
| 3 Half a question | A turn the detector calls unfinished keeps its words for the speaker's next ones (two turns and 60 words at most; dropped when the user speaks or the gap passes) | `continuation_gap_ms` | `test_a_question_left_unfinished_keeps_its_first_half`, `test_an_unfinished_sentence_is_dropped_once_the_user_speaks_or_time_passes` |
| 4 Follow cards | A turn is answered if a question fired since the user last spoke, up to 15 s before the other side's last line; no follow when the user is asking | — | `test_no_follow_card_after_a_remark_that_follows_the_question`, `test_no_follow_card_when_the_user_is_the_one_asking` |
| 5 Broken starts | An application's pack lives 30 days; the badge says when nothing comes from the call | `call_silent_warn_s` (120) | `test_an_applications_pack_outlives_the_day_it_was_built`, `test_the_badge_says_when_nothing_comes_from_the_call` |
| 6 Speaking for the user | Prompt rules: a choice or estimate that is the user's is never made for them; spoken amounts are read sensibly; two more checks need no card | — | `test_the_prompts_leave_the_users_choices_to_them_and_read_amounts_sensibly` |
| 7 Tool bookkeeping | Standup rule and work-log extraction: progress as told to a teammate | — | the standup prompt test, `test_work_log_extraction_leaves_out_tool_bookkeeping` |

**Cost.** None of these adds a model call per card. 1(b) shows a pass that is already
running. 1(a) adds a second detector call when the first has no token after 1.2 s: at the
November rate (11 to 16% of calls in a slow spell, about $0.0002 each on the Priority tier)
that is about $0.02 to $0.03 per live hour in a bad hour, near zero otherwise. The update
card adds one detailed answer per standup (about $0.005). Fewer wrong follow cards save
about $0.005 each (5 in the November sessions).

**1(b) and withdrawn cards.** You ruled out cards that appear and vanish on 2026-10-14;
this brings them back on purpose, narrowly. The local check passes only questions that end
in a question mark, open with a question word or a request to tell, run to eight words, and
are not about pay, location, availability or work status. On the interview fixture no card
was withdrawn. In a real call one will be now and then (a rhetorical question from the
interviewer is the likely case). The end line counts them; `COPILOT_FIRST_SHOW_MIN_WORDS=`
(empty) turns it off without a code change.

**Not changed.** Why app 0.2.4 captured almost nothing on two sessions (the desktop would
have to report its devices and levels). Cards for checks that need only a yes. The five
old commitments without a date (a clean-up on the stage needs your OK). Existing work
items keep their bookkeeping until their log is extracted again.

**Not measured live.** The latency effect of 1(a) and 1(b), and the update card with the
real pack and microphone: the next interview and the next standup.
