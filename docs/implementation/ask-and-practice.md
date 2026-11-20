# Ask and interview practice

Updated 2026-11-19. Both experiences use the existing signed-in account, selected profile and API. They do not start a live call.

## Desktop entry points

- **Work context → Ask a question** opens the saved conversation workspace. Company, application and saved-call actions can preselect context. Main navigation stays Call, Interview prep, Work context and Past calls.
- **Interview prep → Practice interview** opens setup and saved practice history. An application's Practice action preselects that application. Saved transcript questions can be imported from Past calls.
- Returning to the parent page retains the conversation draft or practice session. Leaving practice stops local audio and pauses its session; starting a real call waits for practice audio release and a successful pause. Account/profile changes remount private workspaces and discard their entry context.

## Configuration

Apply migrations through 0026 using the existing migration runner. The API needs its usual configured model provider, account encryption and PostgreSQL permissions. Ask and practice use the existing model routing; no model keys are shipped to desktop or mobile.

Set `DEEPGRAM_API_KEY` on the API for microphone transcription and generated interviewer speech. `COPILOT_PRACTICE_TTS_MODELS` is a JSON map of language IDs to supported provider voice IDs; the default contains supported English, Spanish, French, German, Italian and Japanese Aura voices. The shared 24-language capability catalog controls spoken selection. All 24 support text practice and Nova-3 microphone transcription; the other 18 use text interviewer questions. Provider capability is separate from speech/device quality certification. See [call-languages.md](call-languages.md).

The gateway's private-brief reuse additionally requires `COPILOT_PERSONAL_AI_API_URL`, as described in [personal-ai-context.md](personal-ai-context.md). Optional calendar preparation setup is in [calendar-provider-setup.md](calendar-provider-setup.md).

## Behavior and data lifecycle

Ask stores encrypted account-owned conversations, questions, answers and bounded source references. Scope can be automatic, general, all authorized records, a company, an application, a document or a saved call, with optional dates. Retrieval includes authorized facts, documents, work logs, saved consenting transcripts, summaries, current private briefs, completed practice and selected cached calendar events. Calendar entries are planned events, not proof of a completed meeting. Public web search is not connected. Each citation is reauthorized when opened; removed, superseded or inaccessible sources fail closed. Transcript sources open the saved call at the supporting segment while keeping the conversation and draft mounted.

Generation streams through the configured model and persists encrypted partial text at most twice per second. Pending previews omit provisional citations; source buttons appear only with the validated final answer. Cancellation, failed/refused generation and retry clear obsolete previews. Requests use stable IDs. The UI polls only while a pending conversation is visible. Results arriving after a different selection cannot replace that conversation or clear its draft. A pending answer is not presented as a completed answer. Deletion removes the encrypted conversation and its messages.

Practice uses a generated plan, one question at a time, answer-dependent follow-ups, bounded duration and rubric-based feedback. Hints and sample answers are explicit; retries retain coaching conditions so comparisons do not treat coached and independent attempts as equivalent. Comparisons show observed improvements, remaining and recurring gaps, and verified excerpts from both answers without another model call. Incompatible rubrics suppress numeric comparisons. Original saved questions retain their source; generated follow-ups are labeled. Setup previews the available profile/application sources. Corrections belong in the original profile/resume/application records, not invented practice facts.

Microphone capture is explicit and separate from live computer audio. Playback stops before microphone recording, and recording stops before playback. The browser audio path requests echo cancellation; no full-duplex or loudspeaker certification is claimed. Captured audio is bounded and transient, sent only for transcription, then released. Transcript and feedback retention requires consent; raw microphone recordings are never retained. Delete removes practice state and cached generated speech. Late permission, playback, transcription and start responses are guarded against leaving the view or starting a real call.

## Validation and remaining external checks

Backend real local-stack tests exercise encrypted lifecycle, ownership, idempotency, cancellation and source deletion. Provider responses are deterministic fixtures. Desktop tests cover draft/source preservation, stale-response rejection, unavailable voice, network retries, consent, preemption and audio cleanup. Rendered pages are checked with axe and narrow viewport overflow checks; artifacts and the reproducible runner are in `docs/design/screens/features` and `docs/design/harness/features-shoot.mjs`.

Physical microphone/headphone/loudspeaker/device switching and Mac audio behavior still require real device trials; the bounded provider compatibility checks below passed with existing credentials. Do not interpret a passing DOM test or native compilation as those trials. Scores describe evidence in a practice answer, not hiring likelihood. No guaranteed cost or latency savings are asserted.

### Deliberate real provider checks (2026-11-19)

Existing configured credentials were available. Two bounded synthetic checks used fictional data, without user recordings or private records:

- Actual practice `voice_request`: Deepgram aura-2-thalia-en generated one English question; nova-3 transcribed the generated MP3 exactly. 32,256 audio bytes held in memory only. Observed single-run TTS 3,909 ms and transcription 2,329 ms; these are measurements, not service guarantees. This validates provider auth/model/media compatibility, not physical microphone, echo, speaker or device behavior.
- Actual core Ask `answer`/ModelRouter streaming: OpenAI gpt-6.1-sol returned a cited fictional Cedar/PostgreSQL answer and nonempty partial text. 243 input/21 output tokens, end_turn, one request without retries. No private source data. Final progressive API persistence/cancellation is separately covered by real local-stack tests.

Practice retry comparisons now add bounded observed improvements, current/recurring gaps and verified answer excerpts, retaining rubric/coaching qualifiers without another model call. The model cannot claim a gap was resolved solely because a later feedback response omitted it.

A bounded real practice model check also passed: two requests generated an interview plan and then validated rubric feedback plus a distinct follow-up from a fictional technical answer. Combined observed duration 13,188 ms. This checks actual adapter/schema compatibility, not interview quality across candidates.
