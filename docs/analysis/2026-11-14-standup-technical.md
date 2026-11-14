# Technical questions in standups, to 2026-11-13: why the material falls short

Every standup on Stage 0 with transcript consent on, 2026-10-26 to 2026-11-13: twelve
sessions at one company, read for the technical turns only (architecture decisions, how a
component works, tradeoffs, estimates, risks, follow-ups on the user's work), and checked
against the company material uploaded by then. As in the earlier documents, no meeting text
is quoted, nobody is named, the company's internals are described in general terms, and
sessions carry labels (ids are in the hand-over note). S1 to S9 are the labels of
[the 2026-11-11 analysis](2026-11-11-multi-session.md); S10 to S12 are new.

## Summary

**35 technical turns were put to the user. 9 got a specific, correct card in time. The
uploaded documents held the needed fact for 25 of the 35, and the card was specific and
correct against them for 8 of those 25.**

| Result | Turns |
| --- | --- |
| Specific and correct, in time | 9 |
| Material never extracted | 9 |
| Not detected | 6 |
| Right fact not retrieved | 4 |
| Wrong type (treated as status) | 3 |
| Too slow | 2 |
| Answer shape wrong for a discussion | 2 |
| Retrieved but lost in the prompt | 0 confirmed (cannot be measured: see section 2) |

The largest cause is upstream of the call: **extraction keeps 6% of what is uploaded.**
Seven documents, 24,400 words, became 69 one-line facts (1,460 words). The documents are
written as briefings, with sections such as "how it works end to end", "decisions with
rationale and rejected alternatives" and "likely questions", and those are exactly the
sections that do not survive: there is no place in memory for a mechanism, a data flow, a
constraint, or the reason and the rejected alternative of a decision. The original text is
never searched.

What the user saw as a result: a calculation rule the first document states in one line
was answered with "I'll verify" (twice, S2); the name of a search index was answered with
another index's name (S12); "how is it built" was answered with a list of constants and a
release order (S12, three times); a teammate's bug report whose cause is one sentence in
the design document got "I'd check where it comes from" (S8).

Two more findings outside the seven classes:

- **Cards for teammates are back in a three-person call.** In S11 (the manager, a
  teammate and the user) 5 of 11 cards answered questions the manager put to the
  teammate. One-to-one standups (S2 to S10) had none.
- **Facts marked as suggestions in a standup.** In S12 every line of five detailed
  answers ends with the interview marker for a suggested detail, on cited facts. The
  standup bullet style's wording mentions the marker, and the model applied it.

## 1. What was read

| Label | Date | Length | Call | Technical turns for the user |
| --- | --- | --- | --- | --- |
| S1 | 10-26 | 38.6 min | Manager questions a teammate's design; the user at the end | 1 |
| S2 | 10-27 | 20.5 min | One teammate reviews the user's report | 4 |
| S3 | 10-28 | 25.5 min | One teammate | 1 |
| S4 | 10-28 | 15.5 min | The manager, on package sizes and shared libraries | 10 |
| S5 | 10-30 | 19.0 min | One teammate, a design discussion | 2 |
| S6 | 11-03 | 14.9 min | One teammate | 3 |
| S7 | 11-04 | 8.6 min | One teammate | 1 |
| S8 | 11-05 | 22.5 min | One teammate, feedback on a preview | 4 |
| S9 | 11-09 | 5.2 min | No call; two typed status questions | 0 |
| S10 | 11-12 | 14.8 min | One teammate | 2 |
| S11 | 11-13 | 29.0 min | The manager, a teammate and the user | 1 |
| S12 | 11-13 | 4.0 min | A mock: six typed questions, no audio | 6 |

Not read: one 6-second restart on 11-04 (no text). No standup has consent off. Interviews
were listed (mode, date, length) and not opened.

**The material.** Seven documents, all Markdown or text, uploaded 10-26 to 11-13:

| Doc | Kind | Words | Uploaded | Facts kept | Share of its words |
| --- | --- | --- | --- | --- | --- |
| D1 | A feature write-up, with "how it works end to end" and "likely standup questions" | 2,294 | 10-26, before S1 | 13 | 12% |
| D2 | A design document: current state, decisions with rejected alternatives, edge cases, FAQ | 6,081 | 10-27 | 14 | 6% |
| D3 | Shared libraries: the placement rule, who imports what, "likely questions" | 2,382 | 10-27 | 18 | 14% |
| D4 | An investigation write-up, written after S4 | 3,273 | 10-28 | 10 | 7% |
| D5 | A product and status briefing (no headings) | 3,147 | 11-13 | 8 | 5% |
| D6 | The same briefing, uploaded again five hours later | 3,634 | 11-13 | 0 | 0% |
| D7 | A technical overview: "how it's built" per feature, limits, "questions he may ask" | 3,574 | 11-13, two minutes before S12 | 6 | 3% |

- Every batch of proposals was approved in one go one to four minutes after its upload.
  The approval step delayed nothing and filtered nothing.
- D6 is a second upload of D5. It did not replace D5: it became a second document, most
  of its facts were dropped as already known, five came out as new proposals, and all five
  were rejected. Both files are still listed.
- D7's "how it's built" section (about 1,500 words on three features) became four
  decisions and two glossary entries, both of them names of constants.

## 2. How this was measured

- **Read only, from the stage:** the twelve sessions' decrypted transcripts, cards and
  answers with `latency_ms`, the company's memory nodes, proposals and documents.
  Everything stayed in a scratch directory outside the repository and was removed from
  the VM afterwards.
- **By hand:** which turns were technical and for the user, what each card said, whether
  it matched the material and what the user then said. One reader's judgement.
- **By script:** for each turn, whether any document uploaded by then states the fact
  (a pattern per turn), whether any memory node of that day does, and what the gateway's
  own search queries return for the turn's text against the memory as of that session
  (full text top 4, as the short answer gets; hybrid top 6, as the detailed answer gets).
  The replay leaves out the two follow-up steps of live search (newest entry per ticket,
  one hop over edges), so it is slightly stricter than the live call.
- **Not recorded, so not measurable:** which nodes each pass was given (answers store a
  count, `memory_hits`, and the nodes cited, not the hits), which is why "retrieved but
  lost in the prompt" has no confirmed case; and whether documents were ticked on the
  start screen (the list lives in the session's live state, which expires).

## 3. Every technical turn

"In material" is as of that session. "Search" is the replay: whether the fact was in the
top results for the turn's own words.

| # | Session | What was asked (paraphrased) | Kind | Fired, as | In material | Extracted | Search | Card | Class |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | S1 | Do you remember the rule about where report-specific code goes? | decision | no | no (uploaded next day) | — | — | none | not detected |
| 2 | S2 | The calculation over a date range is clear, right? | how it works | respond, 3.5 s | D1, one line | no | miss | "clear, I'll check the implementation" | never extracted |
| 3 | S2 | Is that figure an average or a sum? | how it works | no | D1, one line | no | miss | none; the earlier card said to ask for the notes | not detected |
| 4 | S2 | Does this change the plan for the next report; should the dates move? | estimate | no | work log | yes | hit | none | not detected |
| 5 | S2 | You still have the migration to do, right? | follow-up | no | work log | yes | hit | none | not detected |
| 6 | S3 | Can the calculated fields be used as filters, or is that too much? | estimate | technical, 1.5 s | D1 | yes | hit | possible, more than adding fields, why; matched the user | correct |
| 7 | S4 | You know why package size slows a cold start? | how it works | technical, 2.1 s | general knowledge | — | — | right | correct |
| 8 | S4 | Are you familiar with a named framework? | how it works | technical, 1.7 s | general knowledge | — | — | right | correct |
| 9 | S4 | Is there a function you introduced recently? | architecture | standup_status, 1.9 s | D3 | yes | miss | short: two libraries; detailed (4.6 s): the right function | wrong type |
| 10 | S4 | Which function do you mean? | architecture | respond, 2.1 s | D3 | yes | miss | named an unrelated component and said it is not a function | not retrieved |
| 11 | S4 | Does any function use the PDF code? | architecture | technical, 1.9 s | D3 | yes | hit | three components, right | correct |
| 12 | S4 | Does this one import a PDF library? | architecture | technical, 2.0 s | D3 | yes | partly | the two libraries by name, right | correct |
| 13 | S4 | What is that library called? | architecture | technical, 1.7 s | D3 | yes | miss (4 words) | "the name isn't clear", one card after naming it | not retrieved |
| 14 | S4 | If export libraries went into the shared code, remove them at once | risk | respond, 2.6 s | D3 (who imports what) | partly | miss | "I'll check where they are included" | not retrieved |
| 15 | S4 | Without the shared folder you would have to duplicate code, right? | tradeoff | technical, 2.2 s | D3, rule and reason | rule yes, the reason for three libraries no | hit (hybrid) | the rule, then general advice on splitting packages | never extracted |
| 16 | S4 | Could there be a library for one module only? | design | respond on the closing check | D3, "likely questions" | no | miss | restated the concern | never extracted |
| 17 | S5 | Was there a technical reason not to do it? | decision | technical, 6.2 s | work log | yes | miss | right (from the call), after the user had started | too slow |
| 18 | S5 | Where should the image setting live? (four cards over 8 minutes) | design | follow, respond | work log | yes | — | on topic, about 150 words each | shape |
| 19 | S6 | Builder page or preview; is it doable? (five cards) | design | respond | nothing on it | — | — | an opinion as "I'd", consistent across cards | correct |
| 20 | S6 | With the toggle on, can the list show that type? | estimate | standup_status, 1.8 s | work log | yes | hit | the ticket and what it covers | correct |
| 21 | S6 | If filtering on those fields costs effort or performance, check first | estimate, risk | no | D1 | yes | hit (top 6) | none | not detected |
| 22 | S7 | When does this seem doable? | estimate | standup_status, 1.8 s | work log | yes | nothing (5 words) | short: asked the date back; detailed: a date with open defects | shape |
| 23 | S8 | I cannot group or aggregate by those fields in this report type | how it works | no | D2, a decision with its reason | yes | hit, first result | none; the user said they would check | not detected |
| 24 | S8 | Was that id column added on purpose? | decision | standup_status, 2.5 s | D2, work log | yes | hit | right: on purpose, how, why no migration | wrong type |
| 25 | S8 | We could take it out if nothing depends on it | risk | respond, 3.8 s | D2, work log | yes | miss | asked which column was meant | not retrieved |
| 26 | S8 | A link column shows up although its option is off | how it works | respond, late, on a fragment | D2, one sentence gives the condition | no | miss | "I'd check whether it is added separately" | never extracted |
| 27 | S10 | Those fields are missing because of where you read from, right? | how it works | respond, 1.5 s | D2 (write path, reader) | one side fact | partly | one fact, then "I'd trace" | never extracted |
| 28 | S10 | Can we tell the first submission per asset from the data we have? | architecture | technical twice, then forced | D2 | yes | hit | first card general, with no memory; the second and third right | too slow |
| 29 | S11 | Are you comfortable; did you take your code out of the shared folder? | decision | respond, 1.8 s | D3, D4 | yes | hit | the rule and what stays shared, three sources cited | correct |
| 30 | S12 | Tell me about a feature | how it works | other, 1.2 s | D7 | no (status only) | status items | a pull request's contents | wrong type |
| 31 | S12 | How is it built? | how it works | other, 1.0 s | D7, a section on it | no | miss | fields, a release order, an export cap | never extracted |
| 32 | S12 | More on its logic | how it works | other, 1.3 s | D7 | no | miss | two constants and a paging detail | never extracted |
| 33 | S12 | How does it use the search index? | how it works | other, 3.6 s | D7 | no | miss | the same constants | never extracted |
| 34 | S12 | What is the index called? | architecture | other, 1.3 s | D7, one line | no | miss | another feature's index, wrong | never extracted |
| 35 | S12 | What about package size? | follow-up | other, 1.3 s | D4, work log | yes | hit | right | correct |

Technical discussion that was not the user's turn, and so is not counted: the design
review of S1 (five cards for the presenter, known from the 10-26 analysis), a 12-minute
planning discussion in S11 (five cards for the teammate), and a walkthrough of a new
feature in S8 (one card, typed as status).

## 4. The failures by class

### Material never extracted (9: turns 2, 15, 16, 26, 27, 31 to 34)

- **Evidence:** the fact is in a document uploaded before the call and in no memory node.
  Three of the nine are a single line of a document (2, 26, 34); five are a "how it is
  built" section (27, 31 to 33) or a "likely question" with its answer (16).
- **Root cause:** `company_doc.extract` asks for five kinds (project, system, person,
  decision, term), each a title and one line. A document of 6,000 words is read in one
  call and returns about 14 items. Facts whose title is already known are dropped, so a
  design document cannot enrich a system first named in a work log.
- **What it costs:** the card falls back on "I'd check" or on whatever is nearby (a
  constant, a status).

### Not detected (6: turns 1, 3, 4, 5, 21, 23)

- **Evidence:** turns 4 and 5 are the S2 misses that the 10-27 fix addressed (a check
  right after the user spoke), and that shape has not been missed since. Turns 21 and 23
  are a different one, and still open: a limitation or a cost concern raised about the
  user's feature **as a statement**, with no question in it. The user answered both with
  "let me check"; for 23 the material's first search result is the reason.
- **Root cause:** the detector's `respond` covers a request, a reminder or a concern
  "aimed at" the user; a problem reported in the user's work while the teammate shares a
  screen reads as the teammate's own update.

### Right fact not retrieved (4: turns 10, 13, 14, 25)

- **Evidence:** the fact is a node, and search on the turn's words does not return it.
  Three of the four are short or point back ("which one", "what is that called", "take it
  out"): 4 to 10 words with no name in them. Turn 13's four words returned two unrelated
  work items; the short answer said the name was unclear one card after showing it.
- **Root cause:** the query is the utterance alone. The transcript is in the prompt, but
  nothing from it reaches the search.

### Wrong type (3: turns 9, 24, 30)

- **Evidence:** an architecture question and a "was that on purpose" question typed
  `standup_status`; "tell me about it" answered with a pull request's contents.
- **Root cause:** the detector has no type for a decision or a mechanism at work, and the
  standup rule tells both passes to give status. Content was right on 9 and 24 because
  the nodes existed; on 30 the status shape won.

### Too slow (2: turns 17, 28) and shape (2: turns 18, 22)

- Turn 17 hit a speech-to-text stall (6.2 s; the 10-30 fix has held since). Turn 28 was
  asked in two halves: the first card fired on the first half with no memory in it, and
  the user forced a third.
- Turn 18's cards were 150 words (before the 10-30 length fix). Turn 22, an estimate,
  has no shape: the short answer handed the date back, the detailed one promised a date
  next to a list of open defects.

### What worked (9)

- Where a node held the fact and the question named it, the card was right and cited it
  (6, 11, 12, 20, 29, 35). Turn 29 is the model case: a decision question, answered at
  1.8 s with the rule and what stays shared, three document facts cited in the detailed
  answer, matching what the user then said.
- Documents contributed to 4 of the 9 (6, 11, 12, 29); two were general knowledge (7,
  8), one an opinion with nothing to ground it (19), two from the work log (20, 35).

## 5. Latency and honesty on these turns

- **Short answer, first word at the client** on the 21 detected technical cards: 2.0 s at
  p50, 3.8 s at p95; detailed answer about 4.0 s at p50. The same as all standup cards
  (11-11 analysis). Search itself is not the cost: full text took 3 to 29 ms and the
  hybrid query 5 to 98 ms on every card.
- **The short answer often has no memory.** It waits 30 ms for full text; on typed
  questions and on a question's first half the search had not answered yet (turn 28's
  first card and S12's first, both with 0 hits).
- **No card said it did not know.** The prompts forbid it ("never say the data or the
  notes don't cover it"). Of the 17 turns where a document had the fact and the user did
  not get it, 3 had no card (3, 21, 23), 2 stated something wrong (10, 34), and 12 gave a
  status, a side fact or a plan to check, phrased as the user's ("I'd trace", "I'd
  confirm"). A plan to check is safe to say, but it reads the same whether the material
  covers the question or not, so the user cannot tell.
- **Nothing invented was presented as the company's fact** apart from turns 10 and 34,
  where a real fact was attached to the wrong thing.

## 6. What an index over the original text would have found

An offline check with no model: the seven documents cut at their headings into 164 chunks
(156 words on average, each carrying its heading path), searched by keyword only, for 30
search cases: the turns above whose fact, or one closely tied to it, is in a document
uploaded by then, with a turn asked in several pieces counted once per piece. A hit is a
returned chunk that contains the fact. Crude (a simple keyword ranking, one pattern per
case), so read it as a direction.

| Search | Fact in the results |
| --- | --- |
| Memory nodes today, full text, top 4 (what the short answer gets) | 12 of 30, by a looser test (any node on the topic) |
| Chunks, the turn's words, top 4 | 15 of 30 |
| Chunks, the turn's words, top 8 | 25 of 30 |
| Chunks, the turn's words plus the last two minutes of the call, top 4 | 18 of 30 |
| Chunks, top 4 of each of those two queries (8 chunks) | 23 of 30 |

- The text holds what extraction dropped: 25 of 30 with keywords alone.
- Adding the conversation to the query rescues the short, pointing-back turns (2, 13, 17,
  25) and loses others, so it has to be a second query, not a replacement.
- Still missed with 8 chunks: turns where the answer needs a list across sections (which
  functions exist, how a feature is built end to end). Those need a prepared map, not a
  search result.

## 7. What this means for the design

1. Keep the original text and search it; extraction alone cannot be made lossless.
2. Extract structure for what search cannot assemble at question time: the system map,
   decisions with their reason and rejected alternative, flows. Load a compact form of
   it in every prompt.
3. Use the conversation in the search query for short and pointing-back questions.
4. Give decisions, mechanisms, follow-ups and estimates their own types and shapes; a
   standup's status shape is wrong for all four.
5. Let a card say what the material does not cover, and give the safe line.
6. Detect a problem or concern stated about the user's own work.
7. Re-upload must replace; approval adds a step and filters nothing.

**Spend for this analysis:** 37 embedding calls for the search replay, under $0.001. No
model calls, no speech to text.

## 8. What this branch changed

You chose the recommended design, without PDF reading and without speaker separation. Each
part has tests that fail on main.

| Finding | Change | Tests |
| --- | --- | --- |
| Material never extracted (9) | A document is kept whole as sealed sections, searched by keyword in the gateway's memory, and extracted into components, decisions with their reason and rejected alternatives, flows, constraints, risks and terms (`knowledge.index`) | `test_a_company_document_becomes_sealed_sections_and_facts_at_once`, `test_keyword_search_finds_the_one_line_extraction_would_drop`, `test_a_document_is_cut_at_its_headings_and_keeps_their_path` |
| No map to preload | The company digest, rendered from the facts with no model call, its own cached block in every standup and meeting prompt | `test_the_company_digest_is_a_system_map_with_reasons_and_the_same_bytes_each_time`, `test_at_work_the_digest_is_its_own_cached_block_and_a_gap_gets_the_safe_answer` |
| Right fact not retrieved (4) | A short question, or one that points back, is searched again with the company's names from the last lines of the call | `test_a_short_question_that_points_back_is_searched_with_the_calls_names` |
| Wrong type (3), shape (2) | Four work types with their own shapes: decision, how it works, status follow-up, estimate or risk; sent to the app as types it knows | `test_work_questions_have_their_own_types_and_shapes`, `test_a_standup_answers_from_the_companys_documents` |
| Not detected (6) | A problem, limit or cost stated about the user's own work is a turn | the detector rule check in `test_work_questions_have_their_own_types_and_shapes`; live in the eval |
| No card said it did not know | At work, what no entry states is not filled in: the answer gives what is known and says the user will confirm the rest | the prompt check; the eval's two uncovered questions |
| Facts marked as suggestions | The standup and meeting styles no longer name the marker | `test_a_standup_answer_is_never_told_about_the_suggestion_marker` |
| Approval filtered nothing | A document's facts are kept at once; each file shows what it gave | the API and desktop tests |
| Re-upload did not replace | A file of an earlier one's name replaces it once it is indexed | `test_a_new_upload_of_the_same_file_replaces_the_old_one` |
| A document could stick at "Reading" | Any failure marks the file failed with a reason; the scheduler retries hourly for a week, and indexes the documents from before | `test_a_failed_extraction_keeps_the_sections_and_says_so`, `test_documents_without_an_index_are_indexed_hourly` |

Desktop: the Material tab is now Engineering context, with several files per upload, each
file's status, a view of what a file gave, and a box that tests a question and shows the
answer with its type and sources.

**One change from the proposal.** The proposal kept the sections' keyword index in Postgres
and had the gateway open the few sections it returned. The gateway has no grant on the data
keys, and giving it one would widen what a gateway compromise exposes. So the database
holds the sections sealed, with only their vectors readable; a worker opens a company's
sections into Valkey for live calls, where session packs already hold its facts, and the
gateway searches them by keyword in memory.

**Not in this branch:** PDF reading (a PDF is refused at upload with a message), speaker
separation for three-person calls (the cards for a teammate in S11), and "too slow" (turns
17 and 28, covered by the earlier latency work).

## 9. Before and after

`copilot-knowledge-eval` (evals/README.md, "Engineering context") asks the 35 turns of
section 3, moved to a fictional company with invented documents, plus two questions no
document answers. "Before" is main's code on the same fixtures.

| | Before (main) | After |
| --- | --- | --- |
| Detected | 30/37 | 35/37 |
| Type right | 10/37 | 33/37 |
| Fact in the short answer's prompt | 19/31 | 31/31 |
| Fact in the detailed answer's prompt | 24/31 | 31/31 |
| of which preloaded, with no search | 10/31 | 28/31 |
| Short answer correct | 19/33 | 29/33 |
| Detailed answer correct | 20/33 | 30/33 |
| Detailed answer correct, of the cards shown | 20/26 | 30/31 |
| Says it will confirm when nothing covers it | 2/2 | 2/2 |
| Detector, p50 | 1,093 ms | 1,047 ms |
| Short answer's first token, p50 | 1,132 ms | 875 ms |
| Detailed answer's first token, p50 | 1,617 ms | 1,281 ms |
| Short answer's prompt, p50 | 2,984 tokens | 5,911 tokens |

- One run each; answers vary by a question or two between runs, and the latency rows are
  within the provider's drift. What they do show: a prompt twice as long, cached, did not
  make the first token later.
- "Type right" on main is low by construction: it has no type for a decision or a
  mechanism. The answer rows are the ones that compare like with like.
- Still missed after: two concerns said as statements (turns 21 and 26; both fired in an
  earlier run, so the statement rule works on most turns, not on every one), and one
  estimate (turn 4) whose answer did not name the report the plan was about.
- Main's extraction kept 40 facts from the four fixture documents; the new one 30 sections
  and a 10,000-character digest.
- The fixtures are not the real documents and the eval has no audio, so it measures the
  prompts, the search and the models, not timing in a call. The next standup is the check.

**Spend for the build and the eval:** about $0.45. Three runs of the questions (about
$0.10, $0.10 and $0.08) and one two-question check ($0.02) at the configured prices, and two
extraction runs (14k and 9k tokens, and one call that timed out) on the extraction model,
which has no configured price (about $0.12 if it costs what gpt-6.1-sol does). No speech to
text, no new infrastructure.
