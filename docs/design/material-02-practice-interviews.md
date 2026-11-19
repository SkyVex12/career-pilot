# Material 02: Practice interviews

Status: Selected feature saved for future implementation. The implementation details below are proposed requirements; no implementation is started by saving this brief.

Purpose: Let users rehearse interviews grounded in their real experience and a selected job, receive specific feedback, and retry answers with measurable improvement.

## Implementation prompt

```text
Implement Practice Interviews in Career Pilot.

Repository: C:\Users\A\WorkSpace\career-pilot

Inspect the repository instructions and existing session, audio, application, profile, history, feedback, and storage implementations first. Build on the current app and preserve existing user changes. Complete implementation and appropriate validation, rather than stopping at a plan.

1. Practice setup

Provide a clear entry point to Practice Interviews and a compact setup flow.

- Let users choose their profile and optionally an existing application with its job description and resume.
- Permit practice without an application, using the selected profile and a user-specified role or topic.
- Offer appropriate interview types such as recruiter screen, behavioral, technical discussion, and system design.
- Let users choose a target duration, with 15 minutes as the initial default.
- Show which resume, application, and experience will inform the interview, and allow correction before starting.
- Make voice practice the main experience, with a usable text alternative.
- Clearly identify this as a simulated practice interview.

Reuse existing context-selection controls and avoid creating another application-management workflow. Do not require a connected calendar or background call detection.

2. An interactive interviewer

Generate an interview plan from the selected role, interview type, job description, and available candidate context.

- Ask one question at a time.
- Ask relevant follow-up questions based on what the user actually answered.
- Probe vague claims, missing examples, reasoning, tradeoffs, and unresolved details.
- Balance depth with the selected duration; avoid repeating questions or following an irrelevant branch indefinitely.
- Distinguish hypothetical scenarios from factual questions about the user's experience.
- Never invent an achievement, employer, project, metric, or qualification and attribute it to the user.
- Do not claim to reproduce a particular employer's confidential questions or exact hiring process.

Questions may be spoken aloud through the configured speech-generation provider and must also be readable on screen. Keep the interface responsive while generating or speaking.

3. Turn control and practice UI

Provide clear states for interviewer speaking, user answering, processing, paused, and finished.

- Support repeat question, skip question, pause/resume, and finish practice.
- Provide an explicit “I'm done” action for ending an answer.
- If automatic end-of-answer detection is provided, allow natural thinking pauses and let the user override it.
- Support interrupting interviewer speech without creating overlapping questions or orphaned audio playback.
- Distinguish silence or a deliberate pause from a failed or disconnected microphone.
- Let users request a hint or sample answer, and record that assistance was used.
- Keep sample answers hidden during an independent attempt until requested or the attempt is complete.
- Preserve completed turns when the user pauses or an error occurs.

Use the app's current ruby branding, icons, spacing, keyboard behavior, and accessible controls. Keep the active question prominent and the interface compact. Preserve existing live-call and window-mode behavior.

4. Separate practice audio from real-call capture

Introduce an appropriate microphone-only capture path for practice.

The existing call capture opens both microphone and system audio. Do not reuse it unchanged: it would include the generated interviewer voice and unrelated computer audio.

- Store generated interviewer questions as structured interviewer turns, rather than transcribing Copilot's own playback.
- Capture and transcribe the user's microphone only for their spoken responses.
- Where speaker playback is supported, use the generated playback as the echo-cancellation reference or another validated approach that prevents interviewer speech from being recognized as a user answer.
- Test headphones, loudspeakers, echo, interruptions, and microphone changes.
- Prevent practice playback from triggering background call suggestions.
- Handle a transition to a real call explicitly: pause practice, release the microphone when switching, and avoid overlapping sessions.
- Start audio capture only after the user starts practice.
- Stop recording, playback, streaming, and pending generation promptly when practice ends.

Maintain Windows and macOS 13+ compatibility for Apple Silicon and Intel. Use availability checks and compatible fallbacks where necessary.

5. Specific, grounded feedback

After an answer or at the end of a session, provide feedback tied to the actual response.

Evaluate relevant dimensions such as:

- Answering the question asked.
- Clear structure and concise explanation.
- Concrete evidence from real experience.
- Technical reasoning and correctness where it can be assessed.
- Explanation of decisions, tradeoffs, and outcomes.

Give useful excerpts or transcript references showing why a suggestion was made. Separate missing information from a claim that is demonstrably incorrect. Acknowledge uncertainty when evidence or technical context is insufficient.

Suggest improved wording using verified facts only. If a useful detail is missing, ask for it or mark it as a detail the user needs to supply.

Do not grade accent, infer personality, or present a practice score as a prediction of hiring success. If scores are used, define the rubric and retain its version. Do not invent delivery measurements that were not actually calculated.

6. Retry and progress

Let users retry a selected question immediately or save it for later practice.

- Preserve the original response, feedback, assistance used, and each retry as separate attempts.
- Show specific improvements and remaining gaps between attempts.
- Keep coached and independent attempts distinguishable.
- Compare only compatible rubrics and reasonably comparable question difficulty.
- Provide a simple practice history with topics practiced and questions worth revisiting.
- Keep practice records distinguishable from real interview and meeting records.

Progress should explain what changed, rather than relying on a single unexplained number.

7. Practice from previous interviews

Allow users to explicitly choose questions from their own saved interview transcripts as practice material.

- Link each imported question back to the source session and transcript location when available.
- Keep retrieval within the selected profile and authorized records.
- Handle missing, deleted, or unavailable transcripts gracefully.
- Preserve the original question and identify any generated variations or follow-ups.
- Do not imply access to calls that were not saved or authorized.

8. Reliability, data, and integration

Reuse existing authentication, account/profile context, backend services, transcript consent, secure storage, and deletion mechanisms where appropriate.

- Persist completed turns and attempts so a temporary connection failure does not lose the session.
- Define retry behavior for transcription, speech generation, and feedback without duplicating questions, answers, or charges.
- Keep text mode usable when voice features are unavailable, while explaining any backend outage honestly.
- Make retention of raw microphone audio an explicit choice; distinguish it from saving transcripts and feedback.
- Provide deletion of a practice session and its associated retained data through the existing lifecycle.
- Keep provider credentials out of the frontend and avoid unnecessary idle connections.
- Document any additional production provider configuration needed. Do not present mocked speech or feedback as a production integration.

Do not automatically turn tentative practice answers into approved career facts or modify the user's resume.

9. Validation and deliverables

Add meaningful tests for:

- Setup and context isolation between profiles/applications.
- Interview planning, adaptive follow-ups, turn order, and duration handling.
- Repeat, skip, hints, interruptions, pause/resume, finish, and retries.
- Keeping generated interviewer speech out of the user's transcript.
- Microphone loss, echo handling, real-call transitions, and cleanup.
- Grounded feedback, retained attempt history, and rubric compatibility.
- Prior-question imports and deleted source records.
- Offline/provider failures, persistence, deletion, and text fallback.
- Windows and macOS compatibility.

Use deterministic fixtures for routine tests instead of making paid live model calls. Clearly distinguish automated tests, native builds, and actual microphone/playback testing. Keep CI efficient and avoid duplicate Mac packaging builds.

Finish with the working implementation, appropriate validation results, concise setup documentation, and any production configuration or real-device testing still needed.

Resolve routine implementation decisions independently and ask only for missing information that genuinely blocks progress.
```
