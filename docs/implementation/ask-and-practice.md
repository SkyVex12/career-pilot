# Ask and interview practice

Updated 2026-11-19. Both experiences use the existing signed-in account, selected profile and API. They do not start a live call.

## Desktop entry points

- **Work context → Ask a question** opens the saved conversation workspace. Company, application and saved-call actions can preselect context. Main navigation stays Call, Interview prep, Work context and Past calls.
- **Interview prep → Practice interview** opens setup and saved practice history. An application's Practice action preselects that application. Saved transcript questions can be imported from Past calls.
- Returning to the parent page retains the conversation draft or practice session. Leaving practice stops local audio and pauses its session; starting a real call waits for practice audio release and a successful pause. Account/profile changes remount private workspaces and discard their entry context.

## Configuration

Apply migrations through 0026 using the existing migration runner. The API needs its usual configured model provider, account encryption and PostgreSQL permissions. Ask and practice use the existing model routing; no model keys are shipped to desktop or mobile.

Set `DEEPGRAM_API_KEY` on the API for microphone transcription and generated interviewer speech. `COPILOT_PRACTICE_TTS_MODELS` is a JSON map of language IDs to supported provider voice IDs; its default is `{"en":"aura-2-thalia-en"}`. A language also must be enabled in the verified call-language catalog. Text practice remains available when speech is unavailable. Do not enable an untested language simply because the provider accepts its identifier. See [call-languages.md](call-languages.md).

The gateway's private-brief reuse additionally requires `COPILOT_PERSONAL_AI_API_URL`, as described in [personal-ai-context.md](personal-ai-context.md). Optional calendar preparation setup is in [calendar-provider-setup.md](calendar-provider-setup.md).

## Behavior and data lifecycle

Ask stores encrypted account-owned conversations, questions, answers and bounded source references. Scope can be automatic, general, all authorized records, a company, an application, a document or a saved call, with optional dates. Retrieval includes authorized facts, documents, work logs, saved consenting transcripts, summaries, current private briefs, completed practice and selected cached calendar events. Calendar entries are planned events, not proof of a completed meeting. Public web search is not connected. Each citation is reauthorized when opened; removed, superseded or inaccessible sources fail closed. Transcript sources open the saved call at the supporting segment while keeping the conversation and draft mounted.

Generation runs asynchronously with visible pending state, cancellation and retry using stable request IDs. The UI polls only while a pending conversation is visible. Results arriving after a different selection cannot replace that conversation or clear its draft. A pending answer is not presented as a completed answer. Deletion removes the encrypted conversation and its messages.

Practice uses a generated plan, one question at a time, answer-dependent follow-ups, bounded duration and rubric-based feedback. Hints and sample answers are explicit; retries retain coaching conditions so comparisons do not treat coached and independent attempts as equivalent. Original saved questions retain their source; generated follow-ups are labeled. Setup previews the available profile/application sources. Corrections belong in the original profile/resume/application records, not invented practice facts.

Microphone capture is explicit and separate from live computer audio. Playback stops before microphone recording, and recording stops before playback. The browser audio path requests echo cancellation; no full-duplex or loudspeaker certification is claimed. Captured audio is bounded and transient, sent only for transcription, then released. Transcript and feedback retention requires consent; raw microphone recordings are never retained. Delete removes practice state and cached generated speech. Late permission, playback, transcription and start responses are guarded against leaving the view or starting a real call.

## Validation and remaining external checks

Backend real local-stack tests exercise encrypted lifecycle, ownership, idempotency, cancellation and source deletion. Provider responses are deterministic fixtures. Desktop tests cover draft/source preservation, stale-response rejection, unavailable voice, network retries, consent, preemption and audio cleanup. Rendered pages are checked with axe and narrow viewport overflow checks; artifacts and the reproducible runner are in `docs/design/screens/features` and `docs/design/harness/features-shoot.mjs`.

Actual provider speech quality, microphone/headphone/loudspeaker/device switching and Mac audio behavior still require configured provider credentials and physical devices. Do not interpret a passing DOM test or native compilation as those trials. Scores describe evidence in a practice answer, not hiring likelihood. No guaranteed cost or latency savings are asserted.
