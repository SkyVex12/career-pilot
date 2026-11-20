# Six-feature implementation progress

Status: all feasible source implementation, integration and validation are complete after the user's mobile crash/reconnection reports and expanded 24-language requirement. PR #98 remains merged; draft PR #100 carries the completed follow-up on codex/mobile-local-http-testing. Scoped Android native recovery acceptance, secure Android/iOS simulator builds and Mac Universal packaging passed. No production proxy deployment or physical-device stability is claimed.

## Integration and preservation

- Current follow-up branch: codex/mobile-local-http-testing (from cleaned main 226712b); original integration branch: codex/six-feature-program; worktree: C:/Users/A/WorkSpace/career-pilot
- Base: ca13ba8 on cleaned main (user requested move). Includes merged desktop redesign and answer improvements. Original isolated worktree retained, unused. No prior code changes lost.
- Requirement documents copied byte-for-byte from source workspace.
- Next action: review draft PR #100 and follow the explicit activation/release requirements in `six-feature-delivery.md`. No source implementation is queued. Current mobile product source is ed4ef21; integrated product and sourced-host validation head is d8b9048. The delivery reconciliation changes documentation/evidence and a deterministic test assertion only; PR checks report its current status. Production deployment remains separate.
- Resume: Set-Location C:/Users/A/WorkSpace/career-pilot; git status --short
- Validation: deterministic, desktop and real local-stack tests now passing (milestones below). Live provider/device coverage remains separate. Docker Desktop found under AppData and local compose stack started; earlier database blocker resolved.
- Decision: the user superseded the earlier small-language preference with 24 popular practical call languages. Publish only recognition/model-compatible catalog entries, distinguish text/transcription from voice playback, and report speech-quality evidence separately from deterministic implementation.

## Completion checkpoint — 2026-11-20

- All feasible engineering milestones are complete. Source milestones: `2a2cd77` backend/languages/resume/proxy, `24a76a6` desktop integration (including PR99 fixes), `7956ac6` mobile redesign/runtime, `a9af830` meeting drafts/keyboard/landscape, `5caa61e` malformed-event recovery and content navigation, `b3736ae` ignoring queued socket frames after terminal failure, `ed4ef21` shared grant retirement and recoverable practice setup, `d8b9048` real sourced-host→Ask verification.
- Confirmed deployed defect: public `/v1/companion` returned HTTP404 while `/v1/session` upgraded. Source now forwards companion WebSocket upgrades separately from REST routes and preserves the original host. Real local nginx handshake/replay/host checks passed. The public proxy has not been deployed by this task.
- Replay, profile-header and runtime fixes: unordered folded answers now replay in sequence; the default profile omits an empty header; operations have bounded deadlines/cancellation; foreground sockets renew relay authentication; malformed events stop the display and expose explicit retry. Existing desktop capture is reused throughout.
- Mobile design now has four primary tabs, focused answer/feedback cards, profile/account sheets, compact landscape rail, accessible controls, large-text/dark styling, and searchable selectors. Ask, Practice, Companion and unsaved meeting drafts survive tab changes within the selected account/profile. Hidden audio/polling pauses; profile/sign-out clears private state.
- Android 14 native acceptance uses only isolated LDPlayer **CareerPilotValidation**, serial **emulator-5556**, fictional localhost services and an embedded Hermes APK with ARM64/x86_64 libraries. The user's LDPlayer instance/account were not changed. Completed: server/auth restore; Ask pending→answer/source/draft; live replay/commands; same-phone grant reuse after process restart; drop/foreground reconnect;503/cancel/15s attach timeout/silent handshake; explicit End confirmation; practice context/language/question/answer/feedback/pause/resume/finish; history question import; meeting draft retention/discard confirmation/context/save; portrait, compact landscape,1.3xtext and dark layout. Final b3736ae invalid-event recovery passed: stale queued host frames cannot re-enable commands; explicit Retry restores answers and a subsequent command executes. The isolated crash buffer is empty; exit records contain only intentional package replacement and force-stop. Receipt and selected screenshots: `evidence/mobile-native-2026-11-20.json`.
- Shared deterministic checks: 74 mobile tests and strict TypeScript pass; 11 fictional native fixture tests pass. Final product-head hosted CI44690567974 passed all 1,926 backend tests (one opt-in nginx test skipped), 489 desktop tests across 51 files, 86 TypeScript protocol tests and Linux/Windows Rust checks; Ruff/format/Pyright, TypeScript and generated types are clean. The earlier local 1,919 unique passes across a Windows temp-directory rerun remain historical evidence. Windows native 102 passed/3 intentionally ignored; Windows preview built. Real provider Spanish/Japanese synthetic speech and multilingual retrieval evidence remains separate from fixtures; Tamil/Telugu/Punjabi cross-language recall limitations are documented.
- Uploaded original resumes are worker-extracted into encrypted, source-hash-bound storage via migration 0027, including lazy older-upload backfill. Real database authorization/invalidation/deletion and bounded parser checks passed. Practice reuses authorized private AI briefs. Calendar corrected application/saved-call context and authoritative empty action lists are integrated and tested.
- Final hosted checks, Android/iOS exports, secure Android native package and unsigned iOS simulator build all passed in run 44690564500 at d8b9048; exact artifacts are recorded in `evidence/mobile-completion-hosted-2026-11-20.json`. Mobile product inputs are unchanged from ed4ef21. Mac Universal run 44686036872 succeeded at 7956ac6 with unchanged current desktop inputs. Earlier b3736ae secure Android and iOS native builds passed and are retained as candidate evidence. Final HTTP-test Android APK: 56,081,446 bytes, SHA256 `d5c911784be74861a074d4e30ec557b006a48ea7809b2fecb0d8deac84931b7b`; ARM64/x86_64 signature/ELF/Hermes/manifest checks passed.
- Final ed4ef21 native targeted checks passed: a setup catalog 503 exposes Retry, which restores 24 selectable languages without remounting; malformed live data disables commands and explicit Retry restores answers, followed by a successful command. Shared retirement tests cover delayed A→B→A cleanup without revoking a replacement grant. The broad emulator suite and exact source revisions remain in the native receipt.
- Sourced personal-AI verification passed: two real Codex runs retrieved a pinned public excerpt, published and refreshed a scoped brief; one genuine Ask answer cited it in 3,797 ms. Source authorization, cross-account denial, replacement, deletion, historical answer retention and disposable database cleanup passed. Seven new harness guards plus the existing scoped tests passed (56 total). See `evidence/personal-ai-sourced-host-2026-11-20.json`. No private repository content was sent.
- Delivery-head CI44692179947 exposed an existing screenshot-test ordering error: its unscoped assertion included the legitimate deep answer to an earlier spoken question. A deterministic reproduction waited for that answer and confirmed the screenshot emitted no deep text. The regression now requires the earlier answer, scopes the no-code assertion to the screenshot question ID, and retains cancellation/consent/transcript-isolation checks. All9 screenshot tests, scoped Ruff/format and Pyright passed. Product code and native artifact inputs are unchanged; the final PR checks cover this test-only correction. The failed run (1,925 passed/1 failed/1 skipped) is retained rather than represented as green.
- The 429-requirement source/evidence reconciliation is complete. Independent final review verified local links, artifact/source hashes and CI counts; the final delivery documentation does not change tested product inputs. External activation remains owner OAuth registrations, compatible backend/migration/worker/proxy deployment and language configuration, HTTPS/WSS for normal releases, host access, production signing, and physical audio/device/conferencing validation. No ANR root cause or all-device stability is inferred from emulator success.

Current requirement reconciliation: **429 unique IDs: 383 implemented, 16 verified, 30 externally blocked**. No requirement is omitted or pending source implementation. “Implemented” records complete code and appropriate local checks, not release certification. M06-032 now includes the 24-question same-language matrix, bounded cross-language tests and one actual synthetic embedding comparison; Tamil/Telugu/Punjabi recall limits remain explicit. M05-047 retains only its remote-host release dependency; the sourced local host→Ask journey is complete.

| Material | Implemented | Verified | Externally blocked |
| --- | ---: | ---: | ---: |
| 01 |63|6|10|
| 02 |80|2|4|
| 03 |70|3|0|
| 04 |72|1|13|
| 05 |53|1|1|
| 06 |45|3|2|

Android emulator evidence supplements M04-070–080; those rows remain externally blocked wherever the original acceptance requires physical devices. No simulator or synthetic provider result is promoted to physical audio certification.

## Requirement ledger

Each row retains original acceptance text. Evidence and files are recorded in milestone entries below.

### Material 01
- **M01-001** [implemented] Implement background call detection and optional calendar-based meeting preparation in Career Pilot.
- **M01-002** [implemented] 1. Background operation
- **M01-003** [implemented] Add an optional background mode for Windows and macOS 13 Ventura and newer, supporting both Apple Silicon and Intel in the existing Universal Mac build.
- **M01-004** [implemented] Let users enable “Launch at login” and “Keep running in the background.”
- **M01-005** [implemented] Provide a ruby icon in the Windows system tray or Mac menu bar.
- **M01-006** [implemented] Closing the main window should keep the app running when background mode is enabled.
- **M01-007** [implemented] Provide tray/menu-bar actions to open Copilot, pause/resume call suggestions, and exit completely.
- **M01-008** [implemented] Keep idle operation lightweight. Prefer native OS events over frequent polling.
- **M01-009** [implemented] Background startup must not wake the GCP backend, open an audio capture session, or establish a live transcription connection.
- **M01-010** [implemented] Preserve existing manual app startup and call behavior.
- **M01-011** [implemented] 2. Detect calls without browser extensions
- **M01-012** [implemented] Implement detection entirely in the desktop app. Do not require or build a Chrome extension.
- **M01-013** [implemented] Use native audio-session/device activity, application identity, and other appropriate OS signals to identify likely calls. Audio activity alone must not be treated as proof of a meeting.
- **M01-014** [externally blocked] Support targets:
- **M01-015** [externally blocked] Desktop apps: Zoom, Microsoft Teams, Slack, Discord, WhatsApp, and FaceTime where available.
- **M01-016** [externally blocked] Browser meetings: Google Meet, Zoom Web, and Teams Web in major browsers, without extensions.
- **M01-017** [externally blocked] Android emulators: BlueStacks and LDPlayer, including multiple running instances.
- **M01-018** [externally blocked] Phone linking: Microsoft Phone Link on Windows and iPhone call relay on Mac.
- **M01-019** [verified] Evaluate Samsung Flow, AirDroid, and Lenovo/Motorola Smart Connect for additional integrations.
- **M01-020** [implemented] Separate these capabilities:
- **M01-021** [externally blocked] Detecting an incoming call.
- **M01-022** [externally blocked] Detecting an active call.
- **M01-023** [externally blocked] Accessing the call’s audio through the computer.
- **M01-024** [implemented] Use application-specific adapters where necessary. Preserve macOS 13 compatibility through availability checks and fallbacks.
- **M01-025** [implemented] When the exact browser service, Android app, caller, or meeting title cannot be identified reliably, use a generic “Call detected” message. Do not invent metadata.
- **M01-026** [implemented] Detection must work without recording or transcribing audio. Avoid requiring screen capture just to monitor for calls.
- **M01-027** [implemented] 3. Call suggestion experience
- **M01-028** [implemented] When a likely call starts, show one small suggestion without stealing focus:
- **M01-029** [implemented] Debounce brief activity and filter microphone tests, dictation, media playback, and Copilot’s own audio.
- **M01-030** [implemented] Show at most one suggestion for the same call.
- **M01-031** [implemented] Dismissing it should suppress further suggestions for that call.
- **M01-032** [implemented] Muting, unmuting, changing devices, switching windows, or reconnecting briefly must not repeatedly trigger suggestions.
- **M01-033** [implemented] Do not suggest starting another session while Copilot already has an active session.
- **M01-034** [implemented] Respect notification preferences and Do Not Disturb.
- **M01-035** [implemented] Allow suggestions to be paused temporarily or disabled per application.
- **M01-036** [implemented] Audio capture and backend session creation begin only after the user accepts.
- **M01-037** [implemented] When the external call appears to end, offer to end the Copilot session. Do not end it just because participants are silent, the microphone is muted, or there is a brief disconnection.
- **M01-038** [implemented] 4. Optional calendar connection
- **M01-039** [implemented] Add optional Google Calendar and Microsoft Outlook calendar connections.
- **M01-040** [implemented] Use the minimum read-only calendar permissions needed.
- **M01-041** [implemented] Let users choose which calendars Copilot uses.
- **M01-042** [implemented] Support disconnecting an account and removing its cached calendar data.
- **M01-043** [implemented] Handle expired permissions, revoked access, offline operation, and provider errors gracefully.
- **M01-044** [implemented] Store credentials using the existing secure credential-storage approach.
- **M01-045** [implemented] Never embed a client secret in the desktop frontend or store tokens in plain browser storage.
- **M01-046** [implemented] Keep calendar synchronization bounded and lightweight; it must not keep the GCP backend awake unnecessarily.
- **M01-047** [implemented] If production OAuth configuration is unavailable, complete the integration and tests, clearly identify the missing configuration, and keep the rest of the feature usable. Do not present mock calendar data as a working production connection.
- **M01-048** [implemented] 5. Meeting preparation
- **M01-049** [implemented] Use the user’s connected calendars to improve preparation and session context.
- **M01-050** [implemented] Before “Interview with Acme,” suggest the matching application, job description, and resume.
- **M01-051** [implemented] Suggest the calendar event title for the Copilot session.
- **M01-052** [implemented] For a recurring meeting, surface relevant previous notes and unfinished action items when those exist.
- **M01-053** [implemented] Provide a compact preparation view with the meeting time, suggested context, and an easy way to change it.
- **M01-054** [implemented] 6. UI consistency
- **M01-055** [implemented] Put background and detection preferences in the appropriate existing settings area.
- **M01-056** [implemented] Provide clear calendar connection and synchronization states.
- **M01-057** [implemented] Use concise labels and small help tooltips where needed.
- **M01-058** [verified] Preserve the existing large, medium, and minimal window modes.
- **M01-059** [implemented] Do not reintroduce removed sections, duplicate call controls, unnecessary scrolling, or large explanatory blocks.
- **M01-060** [verified] Keep the existing drag behavior, quick settings, overlay, and profile selection working.
- **M01-061** [implemented] 7. Validation
- **M01-062** [implemented] Add meaningful tests for:
- **M01-063** [implemented] Background startup, closing, restoring, exiting, and single-instance behavior.
- **M01-064** [verified] Detection state transitions, false positives, deduplication, dismissal, pause/resume, and reconnects.
- **M01-065** [verified] Excluding Copilot’s own microphone and audio activity.
- **M01-066** [verified] Starting through a suggestion using the existing session flow.
- **M01-067** [implemented] Calendar authorization failures, disconnects, matching, recurrence, cancellations, and time-zone handling.
- **M01-068** [implemented] Preventing capture or backend-session creation before user acceptance.
- **M01-069** [externally blocked] Windows and macOS compatibility, including guarded APIs for macOS 13.
- **M01-070** [externally blocked] Validate actual call behavior on available platforms and applications. Clearly distinguish automated tests, native compilation, and real-device testing.
- **M01-071** [implemented] Do not claim an application is supported solely because its process name is recognized. Record whether incoming-call detection, active-call detection, and usable call audio were actually verified.
- **M01-072** [implemented] Keep CI efficient. Preserve manual/release-only Mac packaging, avoid duplicate native builds, and reuse verified artifacts where appropriate.
- **M01-073** [implemented] 8. Deliverables
- **M01-074** [implemented] Finish with:
- **M01-075** [implemented] The working implementation.
- **M01-076** [implemented] Appropriate automated tests and validation results.
- **M01-077** [implemented] Setup documentation for background mode and calendar providers.
- **M01-078** [implemented] A concise compatibility matrix showing verified support and remaining platform limitations.
- **M01-079** [implemented] Any production configuration or hardware testing still needed.

### Material 02
- **M02-001** [implemented] Implement Practice Interviews in Career Pilot.
- **M02-002** [implemented] 1. Practice setup
- **M02-003** [implemented] Provide a clear entry point to Practice Interviews and a compact setup flow.
- **M02-004** [implemented] Let users choose their profile and optionally an existing application with its job description and resume.
- **M02-005** [implemented] Permit practice without an application, using the selected profile and a user-specified role or topic.
- **M02-006** [implemented] Offer appropriate interview types such as recruiter screen, behavioral, technical discussion, and system design.
- **M02-007** [implemented] Let users choose a target duration, with 15 minutes as the initial default.
- **M02-008** [implemented] Show which resume, application, and experience will inform the interview, and allow correction before starting.
- **M02-009** [implemented] Make voice practice the main experience, with a usable text alternative.
- **M02-010** [implemented] Clearly identify this as a simulated practice interview.
- **M02-011** [implemented] Reuse existing context-selection controls and avoid creating another application-management workflow. Do not require a connected calendar or background call detection.
- **M02-012** [implemented] 2. An interactive interviewer
- **M02-013** [implemented] Generate an interview plan from the selected role, interview type, job description, and available candidate context.
- **M02-014** [implemented] Ask one question at a time.
- **M02-015** [implemented] Ask relevant follow-up questions based on what the user actually answered.
- **M02-016** [implemented] Probe vague claims, missing examples, reasoning, tradeoffs, and unresolved details.
- **M02-017** [implemented] Balance depth with the selected duration; avoid repeating questions or following an irrelevant branch indefinitely.
- **M02-018** [implemented] Distinguish hypothetical scenarios from factual questions about the user's experience.
- **M02-019** [implemented] Never invent an achievement, employer, project, metric, or qualification and attribute it to the user.
- **M02-020** [implemented] Do not claim to reproduce a particular employer's confidential questions or exact hiring process.
- **M02-021** [implemented] 3. Turn control and practice UI
- **M02-022** [implemented] Provide clear states for interviewer speaking, user answering, processing, paused, and finished.
- **M02-023** [implemented] Support repeat question, skip question, pause/resume, and finish practice.
- **M02-024** [implemented] Provide an explicit “I'm done” action for ending an answer.
- **M02-025** [implemented] If automatic end-of-answer detection is provided, allow natural thinking pauses and let the user override it.
- **M02-026** [implemented] Support interrupting interviewer speech without creating overlapping questions or orphaned audio playback.
- **M02-027** [implemented] Distinguish silence or a deliberate pause from a failed or disconnected microphone.
- **M02-028** [implemented] Let users request a hint or sample answer, and record that assistance was used.
- **M02-029** [implemented] Keep sample answers hidden during an independent attempt until requested or the attempt is complete.
- **M02-030** [implemented] Preserve completed turns when the user pauses or an error occurs.
- **M02-031** [verified] Use the app's current ruby branding, icons, spacing, keyboard behavior, and accessible controls. Keep the active question prominent and the interface compact. Preserve existing live-call and window-mode behavior.
- **M02-032** [implemented] 4. Separate practice audio from real-call capture
- **M02-033** [implemented] Store generated interviewer questions as structured interviewer turns, rather than transcribing Copilot's own playback.
- **M02-034** [implemented] Capture and transcribe the user's microphone only for their spoken responses.
- **M02-035** [externally blocked] Where speaker playback is supported, use the generated playback as the echo-cancellation reference or another validated approach that prevents interviewer speech from being recognized as a user answer.
- **M02-036** [externally blocked] Test headphones, loudspeakers, echo, interruptions, and microphone changes.
- **M02-037** [implemented] Prevent practice playback from triggering background call suggestions.
- **M02-038** [implemented] Handle a transition to a real call explicitly: pause practice, release the microphone when switching, and avoid overlapping sessions.
- **M02-039** [implemented] Start audio capture only after the user starts practice.
- **M02-040** [implemented] Stop recording, playback, streaming, and pending generation promptly when practice ends.
- **M02-041** [externally blocked] Maintain Windows and macOS 13+ compatibility for Apple Silicon and Intel. Use availability checks and compatible fallbacks where necessary.
- **M02-042** [implemented] 5. Specific, grounded feedback
- **M02-043** [implemented] Answering the question asked.
- **M02-044** [implemented] Clear structure and concise explanation.
- **M02-045** [implemented] Concrete evidence from real experience.
- **M02-046** [implemented] Technical reasoning and correctness where it can be assessed.
- **M02-047** [implemented] Explanation of decisions, tradeoffs, and outcomes.
- **M02-048** [implemented] Do not grade accent, infer personality, or present a practice score as a prediction of hiring success. If scores are used, define the rubric and retain its version. Do not invent delivery measurements that were not actually calculated.
- **M02-049** [implemented] 6. Retry and progress
- **M02-050** [implemented] Let users retry a selected question immediately or save it for later practice.
- **M02-051** [implemented] Preserve the original response, feedback, assistance used, and each retry as separate attempts.
- **M02-052** [implemented] Show specific improvements and remaining gaps between attempts.
- **M02-053** [implemented] Keep coached and independent attempts distinguishable.
- **M02-054** [implemented] Compare only compatible rubrics and reasonably comparable question difficulty.
- **M02-055** [implemented] Provide a simple practice history with topics practiced and questions worth revisiting.
- **M02-056** [implemented] Keep practice records distinguishable from real interview and meeting records.
- **M02-057** [implemented] 7. Practice from previous interviews
- **M02-058** [implemented] Allow users to explicitly choose questions from their own saved interview transcripts as practice material.
- **M02-059** [verified] Link each imported question back to the source session and transcript location when available.
- **M02-060** [implemented] Keep retrieval within the selected profile and authorized records.
- **M02-061** [implemented] Handle missing, deleted, or unavailable transcripts gracefully.
- **M02-062** [implemented] Preserve the original question and identify any generated variations or follow-ups.
- **M02-063** [implemented] Do not imply access to calls that were not saved or authorized.
- **M02-064** [implemented] 8. Reliability, data, and integration
- **M02-065** [implemented] Reuse existing authentication, account/profile context, backend services, transcript consent, secure storage, and deletion mechanisms where appropriate.
- **M02-066** [implemented] Persist completed turns and attempts so a temporary connection failure does not lose the session.
- **M02-067** [implemented] Define retry behavior for transcription, speech generation, and feedback without duplicating questions, answers, or charges.
- **M02-068** [implemented] Keep text mode usable when voice features are unavailable, while explaining any backend outage honestly.
- **M02-069** [implemented] Make retention of raw microphone audio an explicit choice; distinguish it from saving transcripts and feedback.
- **M02-070** [implemented] Provide deletion of a practice session and its associated retained data through the existing lifecycle.
- **M02-071** [implemented] Keep provider credentials out of the frontend and avoid unnecessary idle connections.
- **M02-072** [implemented] Document any additional production provider configuration needed. Do not present mocked speech or feedback as a production integration.
- **M02-073** [implemented] Do not automatically turn tentative practice answers into approved career facts or modify the user's resume.
- **M02-074** [implemented] 9. Validation and deliverables
- **M02-075** [implemented] Add meaningful tests for:
- **M02-076** [implemented] Setup and context isolation between profiles/applications.
- **M02-077** [implemented] Interview planning, adaptive follow-ups, turn order, and duration handling.
- **M02-078** [implemented] Repeat, skip, hints, interruptions, pause/resume, finish, and retries.
- **M02-079** [implemented] Keeping generated interviewer speech out of the user's transcript.
- **M02-080** [implemented] Microphone loss, echo handling, real-call transitions, and cleanup.
- **M02-081** [implemented] Grounded feedback, retained attempt history, and rubric compatibility.
- **M02-082** [implemented] Prior-question imports and deleted source records.
- **M02-083** [implemented] Offline/provider failures, persistence, deletion, and text fallback.
- **M02-084** [externally blocked] Windows and macOS compatibility.
- **M02-085** [implemented] Use deterministic fixtures for routine tests instead of making paid live model calls. Clearly distinguish automated tests, native builds, and actual microphone/playback testing. Keep CI efficient and avoid duplicate Mac packaging builds.
- **M02-086** [implemented] Finish with the working implementation, appropriate validation results, concise setup documentation, and any production configuration or real-device testing still needed.

### Material 03
- **M03-001** [implemented] Implement Central Ask in Career Pilot.
- **M03-002** [implemented] 1. One shared Ask experience
- **M03-003** [implemented] Add an Ask page to the app's main navigation that works without starting a call. **Superseded placement by explicit user instruction on 2026-11-19: Ask lives inside Work; Practice lives inside Interview prep; no new main tabs.**
- **M03-004** [implemented] Provide one conversation area with an input, streamed or progressively available answers, and follow-up questions.
- **M03-005** [implemented] Let users start a new conversation and return to saved conversations.
- **M03-006** [implemented] Retain drafts and conversation position when opening a source and returning.
- **M03-007** [verified] Use the current ruby branding, icons, compact spacing, and accessible controls.
- **M03-008** [implemented] Keep the main interface simple. Avoid separate tabs for each knowledge source or a large setup form before asking.
- **M03-009** [implemented] Support general explanations, drafting, comparisons, career questions, and questions about the user's available information. Do not imply access to information that has not been provided, saved, or connected.
- **M03-010** [implemented] Which of my projects best fits this role?
- **M03-011** [implemented] What did we decide about authentication?
- **M03-012** [implemented] What commitments are still unfinished?
- **M03-013** [implemented] Help me prepare for tomorrow's interview.
- **M03-014** [implemented] Explain this technical concept.
- **M03-015** [implemented] Help draft a follow-up message.
- **M03-016** [implemented] 2. Automatic, visible context selection
- **M03-017** [implemented] Resumes and approved career facts.
- **M03-018** [implemented] Applications and job descriptions.
- **M03-019** [implemented] Company documents and work logs.
- **M03-020** [implemented] Stored memory, decisions, and commitments.
- **M03-021** [implemented] Saved call summaries and transcripts where available.
- **M03-022** [implemented] Practice interviews from Material 02, when that feature is implemented.
- **M03-023** [implemented] Connected calendar information from Material 01, when connected and available.
- **M03-024** [implemented] Make the selected scope visible and editable.
- **M03-025** [implemented] Preserve an explicit scope across follow-up questions until the user changes it.
- **M03-026** [implemented] Ask a brief clarification when multiple employers, applications, or events make the question materially ambiguous.
- **M03-027** [implemented] Keep account and profile boundaries enforced by the backend, not just by interface filters or prompts.
- **M03-028** [implemented] On a profile change, clearly switch the conversation context; never silently combine another profile's private data.
- **M03-029** [implemented] 3. Reuse existing knowledge and add cross-call retrieval
- **M03-030** [implemented] Reuse the existing Work knowledge question service, company document retrieval, employer memory, and History views where practical.
- **M03-031** [implemented] Extend retrieval to relevant saved transcripts across multiple calls, along with summaries and existing facts. Handle long histories through retrieval rather than placing the entire account history in every model request.
- **M03-032** [implemented] Search only records the user is authorized to access.
- **M03-033** [implemented] Respect whether each call's transcript was stored.
- **M03-034** [implemented] Distinguish original source content, summaries, approved facts, and generated suggestions.
- **M03-035** [implemented] Consider source dates and current commitment status; do not present an old open commitment as current after it has been completed.
- **M03-036** [implemented] Surface conflicting or outdated sources when they change the answer materially.
- **M03-037** [implemented] Keep follow-up conversation context separate from durable approved career facts.
- **M03-038** [implemented] 4. Answers with actionable evidence
- **M03-039** [implemented] Link to the actual application, document, work item, fact, or saved call.
- **M03-040** [verified] For transcript-backed claims, open the relevant call at its supporting timestamp or transcript location.
- **M03-041** [implemented] Include dates where they help distinguish events or changing information.
- **M03-042** [implemented] Extend existing source labels with stable authorized source targets as needed.
- **M03-043** [implemented] Recheck access when opening or retrieving a source, and handle deleted or unavailable records gracefully.
- **M03-044** [implemented] Treat text in retrieved documents, transcripts, and web pages as source content, not as instructions that override the user's request or access restrictions.
- **M03-045** [implemented] 5. Contextual entry points and live-call behavior
- **M03-046** [implemented] Let users open the shared Ask experience from a company, application, or saved call with that item selected as context.
- **M03-047** [implemented] The live overlay remains focused on its current call.
- **M03-048** [implemented] Asking on the central page must not implicitly send a question into, start, or change a running call.
- **M03-049** [implemented] Reuse common answer and source components where useful while preserving the live call's latency and interaction needs.
- **M03-050** [implemented] Opening a cited History or Work item must preserve the Ask conversation and allow a predictable return.
- **M03-051** [implemented] 6. Conversations, drafts, and data lifecycle
- **M03-052** [implemented] Save conversations under the correct account and profile using the existing backend and authentication patterns.
- **M03-053** [implemented] Support deleting a conversation and its retained messages.
- **M03-054** [implemented] Define how source deletion or changed access affects retrieval, cached content, and already-generated conversation excerpts. Explain any retained answer snapshots through the existing data lifecycle rather than implying deletion removes data it does not remove.
- **M03-055** [implemented] Cancel pending generation and preserve the user's draft when interrupted.
- **M03-056** [implemented] Recover from connection failures without duplicate messages or duplicate charges where controllable.
- **M03-057** [implemented] Avoid sending every source or unrelated profile data to the model.
- **M03-058** [implemented] Keep provider credentials out of the frontend.
- **M03-059** [implemented] 7. Validation and deliverables
- **M03-060** [implemented] Validate meaningful user journeys and failure cases:
- **M03-061** [implemented] Asking general questions without a call or documents.
- **M03-062** [implemented] Asking within a selected application or company.
- **M03-063** [implemented] Searching across multiple saved calls with correct source timestamps.
- **M03-064** [implemented] Follow-up questions, context changes, and ambiguous source selection.
- **M03-065** [implemented] Account/profile isolation, changed access, and deleted sources.
- **M03-066** [implemented] Missing transcripts, conflicting dates, and completed commitments.
- **M03-067** [implemented] Grounded answers and honest statements when evidence is insufficient.
- **M03-068** [implemented] Opening sources and returning without losing drafts or conversations.
- **M03-069** [implemented] Separation between central conversations and an active live call.
- **M03-070** [verified] Streaming, cancellation, retries, persistence, and deletion.
- **M03-071** [implemented] Prompt-like text inside retrieved material remaining untrusted source content.
- **M03-072** [implemented] Use deterministic fixtures for routine tests instead of paid live model requests. Keep CI efficient and avoid unrelated native packaging jobs for changes that do not require them.
- **M03-073** [implemented] Finish with the working implementation, appropriate validation results, concise setup documentation, and any production configuration or real-device verification still required.

### Material 04
- **M04-001** [implemented] Implement Career Pilot Mobile for iOS and Android, prioritizing a companion to the existing Windows and macOS desktop app. — Native Android APK and unsigned iOS simulator application compiled successfully; physical installation on iPhone requires owner signing/provisioning.
- **M04-002** [implemented] 1. Headphone-compatible desktop companion: first priority
- **M04-003** [implemented] Support this explicit flow:
- **M04-004** [implemented] The meeting stays on the computer. Headphones and microphone stay connected to the computer.
- **M04-005** [implemented] The desktop captures the selected call audio and microphone using its existing supported capture paths.
- **M04-006** [implemented] The phone displays session events and answers; it does not need to hear room audio or request microphone access for this mode.
- **M04-007** [implemented] Connecting the phone attaches to the existing session. Do not start a second transcription pipeline, generate duplicate answers, or bill a duplicate session merely because another display is connected.
- **M04-008** [implemented] Make the audio-source computer and connection state visible on the phone.
- **M04-009** [implemented] Let the user choose to use the phone as the guidance display while the desktop overlay is hidden. Preserve a way to reopen the overlay or main app.
- **M04-010** [externally blocked] Keep the shared screen free of phone UI unless the user deliberately mirrors it. Describe screen-share behavior through tested configurations rather than promising universal undetectability. — Physical screen-share/mirroring matrix needs desktop and phones; no undetectability claim is made.
- **M04-011** [implemented] Do not quietly fall back to the phone microphone when the desktop disconnects. Explain the interruption and offer an explicit supported alternative.
- **M04-012** [implemented] 2. Account connection and session access
- **M04-013** [implemented] Use the existing account and profile system. Provide a simple way to open the current desktop session on the phone, such as same-account sign-in followed by a visible active-session card.
- **M04-014** [implemented] If QR pairing improves the flow, use a short-lived, single-use pairing exchange with confirmation and revocation. Do not place reusable account credentials or long-lived bearer tokens in QR codes or URLs. — Same-account sign-in and explicit attachment used; QR is unnecessary. Career-OS uses a short-lived, single-use PKCE authorization callback; reusable credentials and bearer tokens never enter URLs.
- **M04-015** [implemented] Enforce account, profile, session, and command permissions on the backend.
- **M04-016** [implemented] Clearly identify the session and selected profile before attaching.
- **M04-017** [implemented] Treat attaching a companion as a separate action from taking over audio capture on another device.
- **M04-018** [implemented] Keep a single audio-capture owner until the user deliberately transfers or ends it.
- **M04-019** [implemented] Reuse existing session-owner and guest-role semantics where appropriate without granting broader permissions than a companion needs.
- **M04-020** [implemented] Disconnect a paired device safely on sign-out or revocation.
- **M04-021** [implemented] Do not record audio merely because a user opens a pairing link or signs in.
- **M04-022** [implemented] 3. A phone interface built for live use
- **M04-023** [implemented] Keep the current question and its answer together in one clear view.
- **M04-024** [implemented] Support portrait and landscape, readable text sizes, dark/light themes, and accessible touch targets. — Responsive native layouts, system theme, adjustable live text, safe areas and 48dp controls; real accessibility/layout device checks remain M04-080.
- **M04-025** [implemented] Let users review previous answers without new content forcibly moving their reading position. Provide a clear return-to-latest action.
- **M04-026** [implemented] Provide compact Answer, ask-a-follow-up, and End call controls.
- **M04-027** [implemented] End call must use the existing confirmation and post-call choices, operate on the same session, and restore the normal desktop page through the established lifecycle.
- **M04-028** [implemented] Keep presentation preferences such as text size local to the device unless the user deliberately synchronizes them.
- **M04-029** [implemented] Offer keeping the screen awake during an active companion session; release that state when the session ends or the user leaves it.
- **M04-030** [implemented] Avoid presenting detailed private answer text in lock-screen notifications by default.
- **M04-031** [implemented] 4. Reliable synchronization and remote actions
- **M04-032** [implemented] Replay missed events or load a current snapshot after reconnecting without duplicating transcript lines or answers.
- **M04-033** [implemented] Distinguish connected, reconnecting, ended, and unavailable states; do not present stale text as a newly received live answer.
- **M04-034** [implemented] Preserve the phone's draft and reading position during connection changes.
- **M04-035** [implemented] Handle Wi-Fi/cellular switching, device sleep, app backgrounding, and returning to the foreground. — Foreground reconnect/backoff and snapshot recovery implemented; physical network-switch matrix remains M04-076.
- **M04-036** [implemented] A phone disconnect must not end the desktop call or lose its ongoing capture.
- **M04-037** [implemented] Respect mobile background limits instead of assuming an indefinitely active socket while suspended.
- **M04-038** [implemented] Make remote commands idempotent where appropriate and acknowledge their actual result.
- **M04-039** [implemented] Resolve simultaneous desktop and phone actions predictably, especially Answer and End call.
- **M04-040** [implemented] Authorize every remote command for the current user and session.
- **M04-041** [implemented] 5. Standalone phone microphone mode
- **M04-042** [implemented] Explain the selected audio source before starting.
- **M04-043** [implemented] Request microphone permission only for a feature that uses it.
- **M04-044** [implemented] Show recording/listening state and offer immediate pause and stop actions.
- **M04-045** [implemented] Preserve the existing transcript-consent, retention, and deletion behavior.
- **M04-046** [implemented] Handle noise, microphone interruption, permission loss, headphones, and incoming calls honestly. — Native error/permission/disconnection stop paths plus explicit typed fallback; real interruption and Bluetooth tests remain M04-079.
- **M04-047** [implemented] If the remote speaker is audible only through headphones on another device, explain that the phone microphone cannot hear them and offer desktop companion mode when available.
- **M04-048** [implemented] Use platform-supported background recording only after an explicit user start, with the platform's required indicators and lifecycle handling. — Deliberately foreground-only microphone; suspend stops capture and no background recording capability is enabled.
- **M04-049** [implemented] Do not promise universal capture of cellular, FaceTime, WhatsApp, Google Meet, or other calls running on the same phone. Treat those as separate app/OS-specific capabilities that require verified API support and real-device testing. Background microphone permission does not establish access to another app's call audio.
- **M04-050** [implemented] Do not bypass operating-system permissions or imply that a mobile app can hide every indicator or detection signal. Test and describe the supported screen-sharing and audio behavior precisely.
- **M04-051** [implemented] 6. Central Ask, practice, and existing information
- **M04-052** [implemented] Extend the mobile app with these shared experiences as their backend capabilities become available:
- **M04-053** [implemented] Central Ask from Material 03, including context scope, saved conversations, grounded answers, and source links.
- **M04-054** [implemented] Spoken practice interviews from Material 02, including turn controls, feedback, retries, and progress.
- **M04-055** [implemented] Existing applications, resumes, and saved call summaries/transcripts for preparation and review. — Call history/transcripts plus authorized application/resume context previews for preparation/practice; original source editing remains desktop.
- **M04-056** [implemented] Calendar-based preparation from Material 01 when a calendar is connected.
- **M04-057** [implemented] Keep account/profile isolation and the same data lifecycle across devices. Opening a source on mobile should preserve the Ask conversation. Practice must keep its generated interviewer audio separate from the user's captured speech, as specified in Material 02.
- **M04-058** [implemented] 7. Data, battery, and production behavior
- **M04-059** [externally blocked] Store authentication secrets using platform-appropriate secure storage and use encrypted authenticated transport. — SecureStore and strict production HTTPS/WSS are implemented; the current Stage0 server exposes HTTP/WS. A separate explicitly labeled Android HTTP test build supports testing only and does not satisfy encrypted production transport. TLS activation and a live secure device trial remain outstanding.
- **M04-060** [implemented] Revalidate access when loading or reconnecting to a session.
- **M04-061** [implemented] Stop unused capture, wake locks, event subscriptions, and generation tasks promptly.
- **M04-062** [implemented] Keep idle and background behavior lightweight; do not wake production services unnecessarily.
- **M04-063** [implemented] Define cache and offline behavior explicitly. Cached answers may be readable when allowed, but generating a new live answer must not appear to work without the necessary connection.
- **M04-064** [implemented] Apply deletion and access changes to mobile caches and source views consistently with the existing backend lifecycle.
- **M04-065** [implemented] Keep model/provider secrets on the backend.
- **M04-066** [implemented] Provide production configuration and install instructions using the actual release channels. Report any signing or store-account requirements that need the user to complete an external step. — README, native projects, EAS internal/production profiles and path-scoped CI supplied; store signing/accounts are external.
- **M04-067** [implemented] 8. Delivery order
- **M04-068** [implemented] 9. Validation and deliverables
- **M04-069** [verified] Add meaningful automated tests for pairing, authorization, profile isolation, attachment versus capture takeover, event ordering/replay, command deduplication, cancellation, session end, reconnect recovery, and cleanup. — Mobile deterministic lifecycle/transport tests plus companion real socket and DB authorization/replay/control tests passed; see backend companion validation.
- **M04-070** [externally blocked] Validate the main workflow on actual devices, including: — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-071** [externally blocked] Windows and macOS desktops paired with both iPhone and Android. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-072** [externally blocked] Supported wired, USB, and Bluetooth headphone/microphone configurations, including device changes. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-073** [externally blocked] Meeting audio from supported desktop apps and browser calls. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-074** [externally blocked] Operation while phone microphone permission is denied in companion mode. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-075** [externally blocked] Simultaneous desktop/phone actions and multiple connected displays. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-076** [externally blocked] Phone lock/unlock, background/foreground, Wi-Fi/cellular changes, desktop sleep, and network loss. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-077** [externally blocked] Clear handling of an ended or deleted session and sign-out/revocation. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-078** [externally blocked] End call confirmation and restoration of the normal desktop page. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-079** [externally blocked] Standalone phone microphone interruptions and unavailable same-phone call audio. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-080** [externally blocked] Portrait/landscape layout, large text, keyboard, and assistive-technology behavior. — Physical Android/iPhone plus Windows/macOS device matrix is unavailable in this environment; implementation and deterministic/server checks do not substitute for this device trial.
- **M04-081** [implemented] Finish with the working mobile apps, desktop/backend integration, validation results, concise setup and release instructions, and any remaining platform-specific limitations or external signing requirements. — Native apps and integration delivered; Android APK signature/bundle verified and iOS simulator build passed. Physical device and production signing checks remain external.
- **M04-082** [implemented] [LockedIn mobile](https://www.lockedinai.com/mobile-app) describes placing a phone beside a laptop to listen to the room. Its page reserves Stealth Mode for the desktop app. — Reference/background material, not a separate implementation deliverable; supported source/foreground limitations documented in mobile README.
- **M04-083** [implemented] [OfferGoose help](https://blog.offergoose.com/post104/) documents mobile microphone listening, the speaker/headphone limitation, and the need for another device when taking a phone call. — Reference/background material, not a separate implementation deliverable; supported source/foreground limitations documented in mobile README.
- **M04-084** [implemented] [Android audio-input sharing](https://developer.android.com/media/platform/sharing-audio-input) explains microphone contention and restrictions on call audio. — Reference/background material, not a separate implementation deliverable; supported source/foreground limitations documented in mobile README.
- **M04-085** [implemented] [Android playback capture](https://developer.android.com/media/platform/av-capture) limits which playback streams can be captured and requires user authorization. — Reference/background material, not a separate implementation deliverable; supported source/foreground limitations documented in mobile README.
- **M04-086** [implemented] [Apple recording audio sessions](https://developer.apple.com/documentation/avfaudio/avaudiosession/category-swift.struct/record) documents recording behavior and interruptions. — Reference/background material, not a separate implementation deliverable; supported source/foreground limitations documented in mobile README.

### Material 05
- **M05-001** [implemented] Implement Context from my AI in Career Pilot.
- **M05-002** [implemented] Start with one complete, tested MCP publishing workflow, two narrowly scoped tools, and one context card. Support additional AI hosts after validating their actual capabilities. Check current official provider documentation at implementation time.
- **M05-003** [implemented] 1. User experience
- **M05-004** [verified] Let a user connect Career Pilot to a supported AI host using an authenticated MCP connection.
- **M05-005** [implemented] Explain the connection in product language: the AI can receive a preparation request and send a brief to the selected Career Pilot project or company.
- **M05-006** [implemented] Support a natural request such as: "Prepare context for tomorrow's standup from this project's recent work and send it to Career Pilot."
- **M05-007** [implemented] Also support publishing from the conversation where work happened: "Update Career Pilot with what we completed here."
- **M05-008** [implemented] Select the intended profile and company/application explicitly, using existing entities where possible.
- **M05-009** [implemented] Show a compact context card with title, preparation time, source count, and View, Refresh, and Remove actions.
- **M05-010** [implemented] View shows the brief, source references, coverage gaps, and editable corrections. Do not make users fill out a long form before importing.
- **M05-011** [implemented] Reuse the brief across the relevant call, practice session, and Central Ask conversation when those features are available.
- **M05-012** [implemented] Keep the core flow usable without calendar access or every other saved material being implemented.
- **M05-013** [implemented] 2. Small MCP interface
- **M05-014** [implemented] get_context_request(request_id): returns the selected context scope, preparation questions, date range, request revision, and expected brief format.
- **M05-015** [implemented] publish_context_brief(request_id, request_revision, idempotency_key, brief): validates and publishes the prepared result for that request.
- **M05-016** [implemented] Authenticate each tool call and derive account/profile/company authority from the stored request and authenticated caller.
- **M05-017** [implemented] Do not trust identity or target ownership supplied in generated brief text.
- **M05-018** [implemented] A repeated publication must not create duplicates. Reject obsolete request revisions and preserve the last usable version if a replacement fails.
- **M05-019** [implemented] Use ordinary code for schema, length, ownership, and version validation. Do not add a model call just to import a brief.
- **M05-020** [implemented] Keep source credentials with the AI host. Career Pilot does not need to copy the user's ChatGPT/Codex credential files or acquire every source-system token.
- **M05-021** [implemented] Expose only the request context needed for this task and a bounded publishing action; the initial tools do not need general account administration or arbitrary execution.
- **M05-022** [implemented] 3. Brief contents and reliability
- **M05-023** [implemented] Store a compact snapshot rather than a complete export of the user's work systems. Depending on the request, it may contain:
- **M05-024** [implemented] Current projects and recent progress.
- **M05-025** [implemented] Decisions and the reasons supported by available evidence.
- **M05-026** [implemented] Blockers, next steps, and outstanding commitments.
- **M05-027** [implemented] Relevant terminology and project names.
- **M05-028** [implemented] Career examples relevant to a selected interview or application.
- **M05-029** [implemented] Source references, source dates where available, preparation time, and coverage gaps.
- **M05-030** [implemented] Preserve source links and enough attribution to inspect the evidence, subject to the source's retention permissions.
- **M05-031** [implemented] Display freshness without treating preparation time as proof that every source is current.
- **M05-032** [implemented] Let users correct or remove individual items and replace or delete an entire brief.
- **M05-033** [implemented] Keep imported context separate from approved resume facts. Importing a brief does not silently certify achievements or edit the resume, work log, or task tracker.
- **M05-034** [implemented] Treat source text and imported instructions as context data, not authority to execute unrelated actions.
- **M05-035** [implemented] Validate claims with targeted evidence or user correction when necessary; avoid running a second autonomous research pipeline on every import.
- **M05-036** [implemented] 4. Calls and reuse
- **M05-037** [implemented] Load the latest eligible brief for the selected profile and company/application.
- **M05-038** [implemented] Use bounded relevant content; do not attach every saved brief to every answer.
- **M05-039** [implemented] Never make live answer generation wait for an external AI preparation task.
- **M05-040** [implemented] Keep refresh separate from the live answer path. A failed refresh preserves the existing brief with its actual preparation time visible.
- **M05-041** [implemented] Publish replacements atomically. Make the active context version clear when a refresh arrives during a call.
- **M05-042** [implemented] Remove deleted or revoked briefs from future retrieval and invalidate affected caches. Follow the existing explicit policy for already saved call artifacts.
- **M05-043** [implemented] Keep private imported context scoped to the authorizing account. Shared profile access alone must not expose it to another account or a guest.
- **M05-044** [implemented] A companion device signed into the same authorized account can use the same context; guest sharing requires an explicit supported sharing policy.
- **M05-045** [implemented] 5. Cost and supported capabilities
- **M05-046** [implemented] Reuse prepared briefs and refresh on deliberate requests in the first version. Add bounded import sizes and cancellation/error handling. Avoid continuous background preparation, bulk source ingestion, or a new vector database solely for this feature.
- **M05-047** [externally blocked] 6. Verification before release
- **M05-048** [implemented] [MCP architecture](https://modelcontextprotocol.io/specification/2026-07-28/architecture): the AI host coordinates models and context; a server exposes focused capabilities and does not receive the full conversation by default.
- **M05-049** [implemented] [MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools): structured tool interfaces for the request and publication operations.
- **M05-050** [implemented] [OpenAI: Add a custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server): ChatGPT supports custom read and write tools, subject to connection and confirmation settings.
- **M05-051** [implemented] [OpenAI: Sign in with ChatGPT](https://learn.chatgpt.com/docs/sign-in-with-chatgpt): using a plan does not grant access to ChatGPT conversations or memories, and usage remains subject to the plan's limits.
- **M05-052** [implemented] Material 01 can supply meeting timing and a preparation prompt when a calendar is connected.
- **M05-053** [implemented] Material 02 can use a relevant brief for practice interviews without inventing career facts.
- **M05-054** [implemented] Material 03 can retrieve permitted briefs as another source in Central Ask.
- **M05-055** [implemented] Material 04 can display assistance using the same authorized session context on mobile.

### Material 06
- **M06-001** [implemented] Implement selectable call languages in Career Pilot.
- **M06-002** [implemented] 1. Selection and defaults
- **M06-003** [implemented] Add one compact Language selector under Customize on the call setup screen, keeping the main toolbar compact.
- **M06-004** [implemented] Save a default per profile and allow an override before starting a call.
- **M06-005** [implemented] Keep English as the default for existing profiles that do not have a selection.
- **M06-006** [implemented] Snapshot the effective language when a call starts. Changing a profile default must not silently change an active session.
- **M06-007** [implemented] For the first release, choose the language before starting rather than switching recognition streams midway through a call. Make this behavior clear in the settings.
- **M06-008** [verified] Switching profiles must load the correct profile's preference and avoid leaking another profile's draft selection.
- **M06-009** [implemented] Use clear language names, including a locale or regional distinction only when that distinction actually changes supported behavior.
- **M06-010** [verified] Preserve current compact window modes, the start-call flow, and overlay behavior.
- **M06-011** [implemented] 2. Supported language catalog
- **M06-012** [implemented] Establish an explicit, backend-validated list of supported call languages.
- **M06-013** [implemented] Verify the configured production speech-recognition model's current language support from official documentation before choosing the initial list.
- **M06-014** [externally blocked] Verify the complete application behavior for each offered language, including question detection, generation, retrieval, and post-call output.
- **M06-015** [implemented] Do not claim support solely because the underlying language model can produce text in that language.
- **M06-016** [implemented] Map stable language identifiers to provider-specific settings in ordinary code. Do not confuse spoken-language selection with existing programming-language controls.
- **M06-017** [implemented] Validate unsupported or malformed values on the backend and provide an actionable message before starting an unusable call.
- **M06-018** [implemented] Apply the selection to every configured recognition path and fallback. Do not silently fall back to English when the selected language is unavailable.
- **M06-019** [implemented] 3. End-to-end propagation
- **M06-020** [implemented] Carry the effective language through the desktop request, protocol, backend session, recognition setup, question detection, answer generation, and retained session metadata.
- **M06-021** [implemented] Persist it through reconnects, resumed sessions, and supported provider failover.
- **M06-022** [implemented] Preserve backward compatibility for older clients and settings records using the established English default.
- **M06-023** [implemented] Keep stored transcripts in their recognized language. Do not translate and overwrite the original transcript.
- **M06-024** [implemented] Adapt question detection beyond English-only keywords or sentence patterns.
- **M06-025** [implemented] Generate answers in the selected language while preserving proper names, code, source identifiers, and quoted evidence where needed.
- **M06-026** [implemented] Keep answer-format choices, such as bullets or paragraphs, independent of spoken language.
- **M06-027** [implemented] Generate the applicable summaries and feedback in the session language, and retain that metadata for History.
- **M06-028** [implemented] Do not label old historical content with a guessed language merely because new calls default to English.
- **M06-029** [implemented] 4. Retrieval and grounding
- **M06-030** [implemented] Check Unicode handling, tokenization, and language-dependent full-text search behavior.
- **M06-031** [implemented] Ensure selecting a supported language does not quietly disable useful resume, application, company, or saved-context retrieval.
- **M06-032** [implemented] Test representative questions with documents in the selected language and with the user's existing documents in another language.
- **M06-033** [implemented] Reuse existing retrieval capabilities where suitable. Add the smallest necessary improvements, rather than creating a separate knowledge store for every language.
- **M06-034** [implemented] Keep citations linked to the original source and preserve account/profile boundaries.
- **M06-035** [implemented] Treat missing evidence as missing evidence; language conversion must not invent experience, decisions, or achievements.
- **M06-036** [implemented] Explain any deliberately limited source-language combinations in the verified support matrix.
- **M06-037** [implemented] 5. Cost and failure behavior
- **M06-038** [implemented] Use the existing recognition and answer-generation path with the selected language configuration.
- **M06-039** [implemented] Avoid adding a separate continual translation service or an extra model request solely to route language settings.
- **M06-040** [externally blocked] Check provider/model compatibility and any material cost differences before enabling a language in production.
- **M06-041** [implemented] Keep brief silence, recognition reconnects, and device changes from resetting the selected language.
- **M06-042** [implemented] Report an unavailable recognition language honestly and preserve the user's selection for the next attempt.
- **M06-043** [implemented] 6. Validation and delivery
- **M06-044** [verified] Use deterministic fixtures for routine tests. Validate settings persistence, profile switching, older-client defaults, rejected identifiers, session propagation, reconnect/failover behavior, prompt language, question detection, citation preservation, and post-call language.
- **M06-045** [implemented] Deliver the implementation, the tested language matrix, meaningful validation results, setup/configuration notes, and any remaining provider or real-device verification needed. Keep subsequent language additions behind the same validation standard.
- **M06-046** [implemented] Material 01 starts calls using the selected profile's language preference.
- **M06-047** [implemented] Material 02 should use the same language identifiers for practice; spoken interviewer output additionally needs a verified speech-generation voice.
- **M06-048** [implemented] Material 03 may reuse the language catalog, but its conversation preferences must not unexpectedly change an active call.
- **M06-049** [implemented] Material 04 displays the existing session's selected language and does not create a second recognition pipeline.
- **M06-050** [implemented] Material 05 can supply evidence in available source languages while the call answer follows the selected language.


## Milestone evidence — 2026-11-19

- M06 language runtime implemented: protocol/Python/TS/Rust regenerated; per-profile users.call_language is separate from synchronized account settings; nullable sessions.spoken_language preserves unknown old history. API catalog and preference validation, Customize UI, immutable session language, both Deepgram channels/reconnect, detector/answer/postcall prompts.
- Language runtime: 149 deterministic tests passed; scoped Ruff and Pyright clean. Call/profile UI regressions: 21 passed. Desktop TypeScript check passed before subsequent Ask UI integration. See call-languages.md. No live speech or macOS claims. Historical checkpoint: Spanish/Vietnamese were deployment-gated then; superseded by the 24-language completion evidence above.
- M01 components: 25 deterministic suggestion/matching tests; 8 native background unit tests; explicit Windows WASAPI activity diagnostic passed (3 external processes, 0 device warnings). It does not establish a call: verified app detection still pending. Tray/background/login UI integration underway.
- M03: encrypted persistent conversation backend and Ask page implemented, integration/testing still in progress.
- Historical initial blocker (resolved): Docker was stopped; local Docker services subsequently started and the complete migrated backend suite passed. Provider/device verification remains separate.
- Historical next action completed; see later milestones and the current next action at the top.

### M05/M06 validation update (2026-11-19)

- M05 encrypted account-private lifecycle, two authenticated MCP tools, revision/idempotency/correction/removal, paste fallback and scoped Ask/practice/live retrieval implemented. Sticky privacy protects historical session artifacts after brief deletion; private sessions reject guests and other profile accounts. See `docs/implementation/personal-ai-context.md`.
- M05 migrated PostgreSQL/RLS workflow: 1 passed (`test_personal_ai_stack.py`). Desktop: 3 passed (`personal-ai.test.tsx`) covering schema/scope fallback, stable retry identity, version-aware correction/removal and failed refresh retention. Typecheck and scoped Ruff passed. Real host connection/write confirmation, remote OAuth registration, production HTTPS sleep/wake and physical companion display remain unverified.
- M06 immutable language, en/es/vi validation gate, both Deepgram channels/reconnect/failover, Unicode detection, language prompts and nullable history metadata implemented. Existing retrieval retained; non-English prepared direct answers disabled because packs lack language metadata. Official nova-3 compatibility and deployment gate documented in `docs/implementation/call-languages.md`.
- Combined personal-AI unit/private-context/language/worker-sessionlog run: 58 passed using live Docker stack. Earlier gateway regression: 149 passed. Historical checkpoint, superseded by the 24-language completion: Spanish/Vietnamese were disabled at that time pending speech/retrieval/deployment evidence. Implemented rows describe code contracts, not provider/device certification.

## Integration milestone: Ask, practice, calendar, personal context (2026-11-19)

- Ask: account-owned encrypted conversations/messages/source references; scoped bounded cross-call retrieval, saved conversation UI, drafts/source drawer, cancellation/retry idempotency. Added private briefs, completed practice evidence, selected cached calendar events to authoritative retrieval. Calendar citations are revalidated on open and describe planned/cached events rather than proof a meeting occurred.
- Practice: encrypted state/operation migration0022, adaptive plan/followups, versioned evidence feedback, independent/coached retry comparison, saved-question history; real provider speech/STT JSON endpoints. Desktop microphone-only recording, half-duplex playback, explicit record/submit, consent for transcript retention, never raw retention, current profile/application preview, saved transcript entry points. Real-call starts await pause and audio release.
- Calendar: Google/Outlook read-only OAuth/PKCE, encrypted rotated tokens/cache, selection/disconnect, bounded sync, meeting-context overrides and recurring notes/actions. Desktop selection and preparation panels integrated; explicit call setup acceptance never joins/captures.
- Automated evidence: focused desktop Ask/practice/audio/language 10 passed; API/core Ask/practice combined 37 passed including 3 real-stack lifecycle cases; calendar agent reports 48 deterministic tests plus 1 real-stack consent/encryption/isolation/disconnect test. Personal context evidence recorded separately below/above by owner. Full desktop regression underway.
- Database tests use disposable migrated databases, local Valkey and object store. Provider answers/auth are deterministic fixtures; no live paid model or calendar consent claimed.
- Remaining: native call evidence adapters/suggestions, mobile native app and authoritative companion transport, actual Windows/macOS audio/device checks, signed mobile builds/real phone workflow, UI visual/accessibility pass, full regression and final requirement reconciliation. No broad feature is marked complete based only on first journey.

### M01 native detection completion update (2026-11-19)

- Implemented Windows WASAPI activity + process identity + app-specific Accessibility button evidence; opt-in, per-app controls, five-second debounce, dismissal/reconnect suppression, own/active/starting-session exclusion, Windows shell notification-state gate, 120-second end review (never automatic stop). No capture/network is started by detection or acceptance. Pause retains dismissed-call identity.
- macOS 13 public NSWorkspace/AX/CoreAudio adapter and UserNotifications permission/delivery/click bridge implemented. Notifications remain under OS Focus/DND control. Source uses no post-Ventura per-process audio APIs or screen capture. Apple Silicon and Intel adapter metadata checks pass against public objc2 bindings using `apps/desktop/native-checks/macos-call-adapter`; this is not full Universal linking or real-device certification.
- Native Windows background suite: 13 passed, 2 host diagnostics excluded by default. Explicit read-only UIA/WASAPI host diagnostic passed (zero call candidates). Suggestion UI: 2 passed for no API/capture on acceptance and no session stop on dismissal. Separate personal-AI UI suite: 3 passed.
- `docs/implementation/background-native-integration.md` records target-specific evidence, official API citations and exact commands. Incoming calls, live conferencing trials, muted/device-switch behavior, DND versions, phone/emulator audio routing, Mac packaging/permissions/hardware remain unverified. Samsung Flow/AirDroid/Smart Connect have no verified adapter and remain excluded. Code-complete rows do not certify these app/device combinations.

### Desktop integration and design revision (2026-11-19)

- User-directed placement: primary navigation remains Call / Interview prep / Work context / Past calls. Ask opens within Work; practice opens within Interview prep. Redesigned compact setup, question/answer workspace, conversation composer, inline context and expandable personal-AI panel reuse ruby design tokens. No independent Ask/Practice main tabs.
- Full desktop Vitest run: **451 passed across49 files**. Release script tests run separately. Deferred-response regressions protect selections, drafts, source views and late practice starts; profile/account entry ownership is checked before remount. Saved-call citations open the actual History component with exact segment/timestamp focus while preserving Ask.
- Rendered Ask, practice and narrow practice: zero axe violations, zero horizontal overflow, no page errors. Reproducible harness and screenshots live under docs/design/harness/features-shoot.mjs and docs/design/screens/features. Browser fixtures are fictional and not production integration evidence.
- Native broad validation: client library81 tests completed, desktop96 passed/3 ignored; strict Clippy for both libraries and workspace rustfmt passed. Windows embedded development executable packaging is underway. Full Mac SDK linking/signing and physical calls are not verified by the two-architecture adapter metadata checks.
- Backend checkpoint **7f66430**: full local-stack suite **1684 passed**, scoped Ruff/Pyright clean. Migration0026 closes cross-request active-brief references; private-context account transfer and stale calendar callback races fixed. See backend-security-audit.md.
- Ask/practice setup and retention guide: ask-and-practice.md. Half-duplex microphone/playback lifecycle is implemented and fixture-tested; physical loudspeaker/headphone/echo/device behavior remains externally blocked. All application/emulator/phone-link matrices require actual accessible applications and call peers before support certification. Calendar OAuth app registrations, real AI-host connection and Mac/iOS signing/device hosts are activation/verification dependencies, not mock successes.
- Remaining at this checkpoint (partial Ask delivery since completed): mobile APK build/integration, final ledger reconciliation and focused draft PR. Windows artifact packaging and mobile native tooling are running locally; no production deployment or user installation performed.

### Mobile native milestone (2026-11-19)
- Material04 native Expo58/RN0.88 clients now include same-account desktop companion, standalone microphone turns, grouped Ask/practice/preparation/history, SecureStore credentials, per-profile lifecycle cleanup and honest foreground-only capture.
- Strict TypeScript, 16 deterministic tests and Android/iOS Hermes export passed. Native Android/iOS projects generated; isolated workflow validates these and optionally builds an Android preview. CI planner 41 tests passed; mobile/native harness paths do not trigger backend deployment, while unknown paths still fail closed.
- Portable JDK17, Android SDK37, Gradle9.4.1 and NDK27 installed with official archive checksums; native Gradle bundled APK build remains in progress. ADB reports no attached devices. Physical devices, macOS/Xcode, store accounts and production signing are external and remain unverified.

### Final feature depth checks (2026-11-19)

- Desktop/native integration checkpoint: **4316aa7**. Progressive Ask backend checkpoint: **93929af** (24 focused tests including5 local-stack cases; strict scoped lint/types pass). Partial answers use real provider streams, encrypted bounded persistence, cancellation/attempt locking and no provisional trusted citation buttons.
- Real English practice adapter smoke passed with configured Deepgram TTS aura-2-thalia-en and STT nova-3. Synthetic question roundtrip exact;32,256 bytes; one TTS call3909ms and one transcription2329ms. Generated bytes kept in memory only. Physical microphone/headphone/echo trials remain unverified.
- Real configured-model Ask smoke passed, fictional sources only, OpenAI gpt-6.1-sol243 input/21 output tokens. Citation and progressive text verified. This is one bounded real provider check, not a grounding benchmark.
- Windows development executable built at target/debug/copilot-desktop.exe (50,902,016 bytes), embedded frontend/custom protocol, unsigned and no updater. No user installation/launch. Initial SHA256 C4433950A4EBE58429AD2CE66193DECB8B5E6D55A44580772C4D005B6E5A336F; a final rebuild will include the explanatory practice comparison UI.
- Additional desktop source/calendar integration checks pass: actual saved-call passage opens and highlights without losing Ask draft; calendar retrieval is disclosure-triggered, consent launch explicit, accepted meeting context does not create or join a session.

- Final desktop regression after comparison feedback: 454 tests across 50 files passed; TypeScript and production frontend build passed.

### Final regression and real-host evidence (2026-11-19)

- Backend final pre-OAuth snapshot: **1,700 tests passed** in 401.17 seconds, global Ruff/format/Pyright and protocol codegen clean. Exact command: `.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=.implementation/backend-final-tmp -o cache_dir=.implementation/pytest-cache --junitxml=.implementation/backend-final.xml`. New OAuth tests will be recorded separately.
- Final Windows executable includes comparison feedback; 50,902,016 bytes, SHA256 **AF003F3FFB75407AB4E076892E3B0BEDACA32AA1B0E96AB7EC2FE92660151DD1**. Replaces the initial artifact hash above.
- Real practice model check: two requests, fictional technical-practice setup/answer only. Actual API model adapter generated a plan, validated rubric feedback and a distinct follow-up. Combined observed duration 13,188 ms, no retry loop; not a quality benchmark.
- Actual installed Codex CLI host connected to the local HTTP MCP server, fetched and published a fictional brief, refreshed it, then verified private retrieval and deletion against a disposable migrated database. Evidence in `target/personal-ai-host/`; remote ChatGPT confirmation/HTTPS behavior remains separate. Consumer-host OAuth implementation is being completed rather than labeled an external-only blocker.
- One final existing Universal Mac preview workflow dispatched on branch commit1b91188: https://github.com/SkyVex12/career-pilot/actions/runs/44653356543. No production publication; platform build result pending.

### MCP OAuth and grant lifecycle completion (2026-11-19)

- Checkpoint **e852e61** adds MCP-only OAuth discovery, registered public clients, explicit email-authenticated same-origin consent, S256 PKCE, atomic short-lived codes, hash-only scoped tokens, account grant limits/expiry and revocation. Ordinary application routes reject these tokens. No new production identity provider or migration was introduced.
- Actual Codex host flow passed again with an OAuth-issued scoped token; redacted durable evidence: `docs/implementation/evidence/personal-ai-host-2026-11-19.json`. This verifies local host tool execution, private retrieval, refresh and removal using fictional data. Remote ChatGPT connection, browser email delivery, host confirmation UX and genuine external source gathering still require configured reachable deployment and host access; M05-047 records that release gate.
- Focused backend35 and desktop6 tests passed, independent security review found no blocking bypass, global Ruff/Pyright clean, all252 Python files formatted and codegen current. This supplements the earlier1700 full-suite snapshot rather than pretending that snapshot included the new OAuth code.
- iOS unsigned simulator compilation dispatched through the existing Mac workflow bridge: https://github.com/SkyVex12/career-pilot/actions/runs/44655330798. New workflows cannot be manually dispatched until registered on the default branch; the explicit bridge runs mobile validation without repeating desktop packaging or publishing.

- Final desktop app after OAuth management: **457 tests passed**, TypeScript and production build clean. Windows embedded debug executable SHA256 **5CFA1C61269DD17178DA7CCEB6BF9D3FA7B5890D9E85DDC8118252E700176255**, 50,902,016 bytes, source e852e61; earlier Windows hashes are superseded.
- Initial Mac run44653356543 passed native tests but its packaging was cancelled after final OAuth controls changed the frontend. Final current-source Universal preview run: https://github.com/SkyVex12/career-pilot/actions/runs/44655879546. No required checks were disabled; this replacement avoids delivering an older interface.

### Mobile artifacts and consent browser verification (2026-11-19)

- Android bundled arm64 development APK built successfully (658 Gradle tasks): 25,670,829 bytes; SHA256 **1d9ffe9b0d2b69737387269645b9aa5b9eff05df0572fb3aacf812f24568372e**. APK v2 signature verifies with the standard Android development certificate. Manifest app.careerpilot.mobile v0.1.0, minSDK24/target36; Hermes bundle and native audio library present. Windows long CMake paths were resolved with short hashed staging directories; the reproducible local Gradle init script is included with mobile setup.
- Actual iOS simulator native build **passed**, workflow44655330798: Xcode26.4, CocoaPods, arm64 Release, CODE_SIGNING_ALLOWED=NO. This is a compiled simulator application, not a signed physical-iPhone app or an actual hardware trial.
- Actual browser consent flow passed against the real served HTML/API and email auth with a MemoryMailer boundary: invalid code, successful sign-in/Allow, Origin/bearer/one-use handle, callback state/issuer/code, PKCE exchange/MCP access, and Deny. Grants revoked after validation. No CSP/page errors; zero axe violations; narrow390px view has no horizontal overflow. Evidence in docs/design/screens/personal-ai-consent; checkpoint373360e.
- Broader post-OAuth backend run observed two existing live-memory searches exceeding their100ms budget under concurrent Android native compilation;1702 passed. Both failures passed unchanged on immediate focused rerun. No assertions or production deadlines were weakened. A clean full run is now executing with local compilation finished.

- Final clean integrated backend regression: **1,704 tests passed in 351.86 seconds**, exit 0, after native compilation finished. No code, assertions or deadlines changed for this rerun. Logs/JUnit: `.implementation/final-oauth-clean.txt` and `.implementation/final-oauth-clean.xml`. Independent OAuth audit and final evidence committed in **b0a1956**.

- Final requirement reconciliation: **429 unique, consecutive IDs**, 383 implemented, 15 verified, 31 externally blocked; no pending/in-progress requirement rows. Original requirement text retained. Delivery README links resolve. Mobile evidence/workaround checkpoint **2d61976**; stable Android artifact receipt and successful iOS simulator CI run linked. Local iOS artifact downloads exhausted bounded TLS-timeout retries, so binary/lockfile inspection is not claimed.

- Final Universal Mac workflow **44655879546 passed** (24m53s), source **e852e61**. Frontend/native tests, Apple bridge availability for both architectures, Universal DMG build, mounted app verification and strict signature verification all passed. Artifact **14594572590**, 27,448,627-byte archive SHA256 **1e8e2253b5c89a665e2ed9426a609e243d30748ff8af542a65bdae7f610e7296**. Preview is ad-hoc signed, not notarized; no production publication. The release configuration pins existing Stage 0 services; compatible backend deployment remains separate.

### Draft integration review

- Draft PR **#98**: https://github.com/SkyVex12/career-pilot/pull/98. Integrated source pushed to `codex/six-feature-program`; no merge/deployment/publication performed. The PR checks provide the authoritative final-head hosted CI result. The final ledger/documentation update is followed by one complete CI pass; already verified native artifacts are not rebuilt for documentation changes.
- Delivery guide: `docs/implementation/six-feature-delivery.md`; Windows/Android stable local artifacts and Mac/iOS GitHub artifacts included. All original requirement files and existing work preserved.

- Hosted CI caught two Linux type errors in the optional Codex/browser validation scripts: Windows-only `subprocess.CREATE_NO_WINDOW` was unguarded. Reproduced with Pyright's Linux target; Windows checks were clean. The helper subprocess flags now select the Windows constant only on Windows, with zero flags on other platforms. Product code and native artifact inputs are unchanged. Final-head hosted CI is rerun after this portability correction; TypeScript/mobile/workflow and both native Rust jobs already passed on the preceding head.

### Post-merge mobile HTTP testing and authentication compatibility (2026-11-19)

- User reported that the installed preview rejects the current HTTP Stage0 server. Added a separate Android `httpTest` variant (`app.careerpilot.mobile.httptest`, **Career Pilot HTTP Test**) with embedded JavaScript, distinct app links/storage, explicit unencrypted-test notice and native HTTP permission. Production remains HTTPS/WSS-only. Shared REST, companion and phone-microphone validation now applies the same transport rules; `/v1` API input normalizes to the origin.
- An idempotent Expo plugin preserves the variant across prebuild. Bundle tasks track public endpoint/flag and dotenv inputs; a native wrapper forces the correct policy after dotenv loading. Two subprocess regressions verify conflicting `.env.production.local` settings cannot change the chosen native policy.
- Read-only current deployment check: Stage0 running, public health200, Google/Career-OS available, email disabled. Setup now verifies `/v1/auth/config` before advancing, shows only configured email auth, and supports the existing Career-OS browser PKCE contract with explicit manual callback paste. Google desktop accounts must link Career-OS on desktop first; native Google client registration remains absent. No passwords handled by this app and no backend configuration changed.
- Career-OS refresh/revocation uses shared `/auth/token/*` routes; the old email aliases return404 when email auth is disabled. Canonical token revocation404 now reports failure rather than falsely claiming revocation. Local credentials still clear immediately.
- Validation: **30 mobile tests passed**, including transport mode matrix, callback origin/state/expiry/single-use, account proof, no-email token lifecycle and native dotenv isolation. Strict TypeScript passed. Independent reviewers checked auth contracts/lifecycle and native variant isolation. Android/iOS secure Hermes exports passed. Android HTTP test compilation, release lint and packaging passed (618 tasks); Windows long paths resolved with local Ninja1.13.2 and the existing object-path workaround. The test variant alone disables shrinking, and bounded Gradle/JVM concurrency avoids local memory exhaustion. No physical-device or real-user browser sign-in success is inferred from these checks.

- Verified HTTP test artifact: `.implementation/artifacts/mobile/CareerPilot-0.1.0-http-test-arm64.apk`, **31,930,539 bytes**, SHA256 **fa46785face54e7370c1466db2d9631397e568837ec09e449a853254371df094**. APK v2 development signature, ARM64/minSDK24/target36, cleartext permission, non-debuggable test identity, distinct deep links, embedded Hermes/current endpoints and native audio library verified. Original APK retained. Receipt is next to the installer; detailed evidence in `apps/mobile/BUILD-VALIDATION.md`.
- Actual mobile-client unauthenticated readiness at 22:54:52 CST reached current Stage0 and confirmed its auth configuration; health200 at 22:55:18 CST. APK prefills the current public endpoints; mobile has no VM wake flow, so open desktop first if asleep. No device installation or real-user sign-in was performed.
- Historical HTTP-test checkpoint reconciliation (superseded by the current table above): **429 unique requirement IDs**, 382 implemented, 15 verified, 32 externally blocked. M04-059 now explicitly records that current Stage0 lacks production TLS; this HTTP test variant does not satisfy that production requirement. Original acceptance text and historical evidence are retained.

- Draft follow-up PR **#100**: https://github.com/SkyVex12/career-pilot/pull/100. APK product source **bca47d7**; machine-readable receipt records that revision and review URL. Branch is pushed, with hosted mobile/CI checks tracked on the PR. No merge, backend deployment, production configuration change or store publication performed.


### Remaining-work integration checkpoint (2026-11-20 UTC)

- Mobile shared navigation and call/Work/practice/history/preparation screens have been redesigned. There are still four primary tabs: Call, Interview prep, Work and History. No new Ask or practice tab was added. Searchable selectors replace long button rows; selectors/source sheets preserve the underlying screen. Runtime malformed responses now surface recoverable errors. Strict TypeScript and53 mobile deterministic tests pass before final device validation.
- PR99 code/test patch (`eed3446`, evidence `0eecc98`) integrated without overwriting newer shared documentation. Current focused notification tests:19 native +19 frontend pass; desktop TypeScript/rustfmt pass. Historical Windows preview is preserved; a combined artifact must be rebuilt because the calendar frontend has changed.
- Independent review caught an additional original-resume gap: uploaded file versions do not store text in `text_enc`. Bounded worker-only extraction and encrypted hash-bound original text are now implemented, including migration 0027, lazy legacy backfill and real migrated RLS/deletion/hash-change tests. No parsing on each Ask request and no production migration performed.
- Actual local nginx routing test upgrades `/v1/companion` (HTTP101) and forwards ready/snapshot/host frames using a fictional upstream. Public Stage0 still returns404; local validation does not imply deployment.
- Android HTTP-test build with native arm64-v8a and x86_64 is in progress. Native/runtime acceptance and the user's ANR cause remain unverified until recorded below.


### Combined backend regression closure (2026-11-20)

- Frozen-source repository run: **1,874 passed, 1 skipped, 45 setup errors in 396.31 seconds**. Every setup error was the same Windows permission failure accessing the pre-existing `Temp/pytest-of-A` directory; no test assertion failed. All affected files were rerun with a fresh contained workspace temporary directory: **132 passed in 42.88 seconds**, including all 45 previously blocked setups. Across these runs, all **1,919 runnable tests passed**; this is combined coverage, not a claim that the first invocation exited successfully.
- Commands: `.venv/Scripts/python.exe -m pytest -q`, followed by the affected 15 files with `-p no:cacheprovider --basetemp=.implementation/pytest-final-20261120-review`. The single opt-in real nginx test is skipped by default and has separate successful actual forwarding evidence in `evidence/companion-nginx-local-2026-11-20.json`.
- Repository-wide Ruff check, Ruff format check (266 files), Pyright (zero errors/warnings) and `git diff --check` pass. Final corrections were limited to typing and formatting in the new resume/proxy tests. The fictional native acceptance fixture and modified resume/proxy checks additionally passed 15 focused tests.
- Uploaded resume original text now uses worker-only bounded extraction, encrypted storage tied to the current content hash, lazy legacy queuing and current-profile/source deletion checks. Synchronous and asynchronous per-request embedding clients both close correctly. Implementation details and backfill limitations: `resume-original-text.md`. No production migration, deployment or provider-quality claim follows from these local regressions.

### Hushed / LDPlayer incoming-call follow-up (2026-11-20)

- Added an explicit Windows desktop opt-in for up to four selected local Android instances. The adapter reads only a package-filtered, redacted posted-notification snapshot and recognizes exact Hushed package + Android CallStyle incoming/ongoing metadata independently of microphone activity. Unknown formats, missing packages and disconnected devices have separate status; no caller/message data or raw snapshots are saved.
- Existing suggestion policy and manual setup remain authoritative: global pause, LDPlayer disable, Windows notification suppression and an active Career Pilot session suppress new offers. Strong incoming signals can prompt immediately on observation; notification replacement, mute, brief loss and dismissal retain one occurrence. ADB failures never prove a call ended. Late UI responses, changed emulator selections and removed configurations cannot accept an old call or strand a prompt.
- Android 14 framework-format validation used only **CareerPilotValidation**, index **1**, serial **emulator-5556**, with the fictional `app.careerpilot.callfixture` package. Incoming/ongoing/screening/message/missed/group/empty states and exact production-package exclusion were collected as sanitized fixtures. The user's instance 0 and real Hushed data were untouched. The fixture's notification was canceled afterward.
- Intermediate focused validation: 52 native background tests passed (2 manual diagnostics skipped), 29 settings tests and 5 suggestion tests passed, strict TypeScript passed. Later lifecycle/process-bound regressions and integration with newly merged main are being validated before delivery; these checkpoint counts are not final-head certification.
- **M01-017 and M01-021 remain externally blocked for actual Hushed acceptance.** Hushed explicitly does not support emulators. Real Hushed incoming/active calls and audio routing remain unverified; polling can miss short calls/gaps, and custom notification formats fail closed. Source and setup: [Hushed call detection](hushed-call-detection.md). This follow-up does not change the original 429 requirement IDs or promote framework fixtures to real-provider verification.

- Final product checkpoint **9985e5d** includes latest main **fdf9523** (PR100 and PR101) and preserves the Windows sharing fixes. Full desktop validation: **550 interface tests in 54 files**, **157 native tests with 6 opt-in/child-fixture tests ignored**, **10 release checks**, strict TypeScript, rustfmt, and strict Clippy for both default and release features passed. Both Apple Silicon and Intel Mac adapter metadata checks passed; these are compile checks, not Mac runtime certification. The final native run includes the configuration-race and package-presence corrections.
- The explicit native ADB diagnostic additionally **passed on CareerPilotValidation**: local discovery, configuration, exact Hushed absence and the `not_installed` adapter status were exercised through the production subprocess runner. This real check initially exposed Android's exit-1 response to `pm path` for an absent package. The implementation now uses a narrowly filtered successful package-list command and exact package matching, with regressions for substring matches and malformed output. The fictional fixture was uninstalled and only validation instance 1 was stopped after testing; the user's running instance 0 remained untouched.
- Windows embedded cloud preview built successfully from **9985e5d**: `.implementation/artifacts/windows/CareerPilot-hushed-detection-preview.exe`, **51,825,152 bytes**, SHA256 **a745140d966aed8373d8bec82ba9b69a5119b6baedf37856cac3f7f17ac3d6e5**. PE x86_64, copy hash and unchanged product source verified; earlier artifacts preserved. This unsigned local preview was not installed or launched. The fixture/native/build evidence and remaining acceptance limits are recorded in [hushed-call-detection-2026-11-20.json](evidence/hushed-call-detection-2026-11-20.json). Branch `codex/hushed-call-detection` is prepared for one focused draft review against main; no deployment, merge or production publication is part of this follow-up.
