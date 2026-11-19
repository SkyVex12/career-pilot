# Material 01: Background call detection and meeting preparation

Status: Agreed feature brief saved for future implementation. Saving this material does not start implementation.

## Implementation prompt

```text
Implement background call detection and optional calendar-based meeting preparation in Career Pilot.

Repository: C:\Users\A\WorkSpace\career-pilot

Build on the existing app and its current UI. Inspect the repository instructions and existing implementation first. Complete the implementation and appropriate validation, rather than stopping at a plan.

1. Background operation

Add an optional background mode for Windows and macOS 13 Ventura and newer, supporting both Apple Silicon and Intel in the existing Universal Mac build.

- Let users enable “Launch at login” and “Keep running in the background.”
- Provide a ruby icon in the Windows system tray or Mac menu bar.
- Closing the main window should keep the app running when background mode is enabled.
- Provide tray/menu-bar actions to open Copilot, pause/resume call suggestions, and exit completely.
- Keep idle operation lightweight. Prefer native OS events over frequent polling.
- Background startup must not wake the GCP backend, open an audio capture session, or establish a live transcription connection.
- Preserve existing manual app startup and call behavior.

2. Detect calls without browser extensions

Implement detection entirely in the desktop app. Do not require or build a Chrome extension.

Use native audio-session/device activity, application identity, and other appropriate OS signals to identify likely calls. Audio activity alone must not be treated as proof of a meeting.

Support targets:

- Desktop apps: Zoom, Microsoft Teams, Slack, Discord, WhatsApp, and FaceTime where available.
- Browser meetings: Google Meet, Zoom Web, and Teams Web in major browsers, without extensions.
- Android emulators: BlueStacks and LDPlayer, including multiple running instances.
- Phone linking: Microsoft Phone Link on Windows and iPhone call relay on Mac.
- Evaluate Samsung Flow, AirDroid, and Lenovo/Motorola Smart Connect for additional integrations.

Separate these capabilities:
- Detecting an incoming call.
- Detecting an active call.
- Accessing the call’s audio through the computer.

Some phone-linking apps only display notifications or control calls while audio stays on the phone. Do not claim full audio support for those cases.

Use application-specific adapters where necessary. Preserve macOS 13 compatibility through availability checks and fallbacks.

When the exact browser service, Android app, caller, or meeting title cannot be identified reliably, use a generic “Call detected” message. Do not invent metadata.

Detection must work without recording or transcribing audio. Avoid requiring screen capture just to monitor for calls.

3. Call suggestion experience

When a likely call starts, show one small suggestion without stealing focus:

“Call detected”
“Start Copilot for live assistance.”
Actions: “Start Copilot” and “Not now”

Show the platform or meeting title only when reliably known.

- Debounce brief activity and filter microphone tests, dictation, media playback, and Copilot’s own audio.
- Show at most one suggestion for the same call.
- Dismissing it should suppress further suggestions for that call.
- Muting, unmuting, changing devices, switching windows, or reconnecting briefly must not repeatedly trigger suggestions.
- Do not suggest starting another session while Copilot already has an active session.
- Respect notification preferences and Do Not Disturb.
- Allow suggestions to be paused temporarily or disabled per application.

Clicking “Start Copilot” should reuse the existing session-start flow, selected profile, and relevant context. Allow correction of the context when needed. If sign-in or audio setup is required, show the necessary existing setup flow.

Once the session starts, hide the main workspace and show the existing overlay. Preserve the overlay’s end-call confirmation and workspace restoration behavior.

Audio capture and backend session creation begin only after the user accepts.

When the external call appears to end, offer to end the Copilot session. Do not end it just because participants are silent, the microphone is muted, or there is a brief disconnection.

4. Optional calendar connection

Add optional Google Calendar and Microsoft Outlook calendar connections.

Call detection must work fully independently of calendar access.

- Use the minimum read-only calendar permissions needed.
- Let users choose which calendars Copilot uses.
- Support disconnecting an account and removing its cached calendar data.
- Handle expired permissions, revoked access, offline operation, and provider errors gracefully.
- Store credentials using the existing secure credential-storage approach.
- Never embed a client secret in the desktop frontend or store tokens in plain browser storage.
- Keep calendar synchronization bounded and lightweight; it must not keep the GCP backend awake unnecessarily.

Inspect the existing authentication architecture before choosing the provider authorization flow. Implement the appropriate desktop/backend integration and document required OAuth configuration.

If production OAuth configuration is unavailable, complete the integration and tests, clearly identify the missing configuration, and keep the rest of the feature usable. Do not present mock calendar data as a working production connection.

5. Meeting preparation

Use the user’s connected calendars to improve preparation and session context.

Examples:

- Before “Interview with Acme,” suggest the matching application, job description, and resume.
- Suggest the calendar event title for the Copilot session.
- For a recurring meeting, surface relevant previous notes and unfinished action items when those exist.
- Provide a compact preparation view with the meeting time, suggested context, and an easy way to change it.

Handle time zones, daylight-saving changes, recurring events, cancellations, declined events, overlapping meetings, and events without meeting links.

Context matching must respect profile boundaries. Do not silently switch profiles or attach an unrelated application based on a weak title match. Let the user confirm or correct ambiguous matches.

Calendar reminders must not automatically join meetings, start recording, or create live Copilot sessions. Do not modify calendar events or send messages.

Call this feature “Meeting preparation” in the UI.

6. UI consistency

Follow the app’s current ruby branding, icons, spacing, controls, and compact presentation.

- Put background and detection preferences in the appropriate existing settings area.
- Provide clear calendar connection and synchronization states.
- Use concise labels and small help tooltips where needed.
- Preserve the existing large, medium, and minimal window modes.
- Do not reintroduce removed sections, duplicate call controls, unnecessary scrolling, or large explanatory blocks.
- Keep the existing drag behavior, quick settings, overlay, and profile selection working.

7. Validation

Add meaningful tests for:

- Background startup, closing, restoring, exiting, and single-instance behavior.
- Detection state transitions, false positives, deduplication, dismissal, pause/resume, and reconnects.
- Excluding Copilot’s own microphone and audio activity.
- Starting through a suggestion using the existing session flow.
- Calendar authorization failures, disconnects, matching, recurrence, cancellations, and time-zone handling.
- Preventing capture or backend-session creation before user acceptance.
- Windows and macOS compatibility, including guarded APIs for macOS 13.

Validate actual call behavior on available platforms and applications. Clearly distinguish automated tests, native compilation, and real-device testing.

Do not claim an application is supported solely because its process name is recognized. Record whether incoming-call detection, active-call detection, and usable call audio were actually verified.

Keep CI efficient. Preserve manual/release-only Mac packaging, avoid duplicate native builds, and reuse verified artifacts where appropriate.

8. Deliverables

Finish with:

- The working implementation.
- Appropriate automated tests and validation results.
- Setup documentation for background mode and calendar providers.
- A concise compatibility matrix showing verified support and remaining platform limitations.
- Any production configuration or hardware testing still needed.

Preserve existing user changes. Resolve routine implementation choices independently and ask only for genuinely missing information that blocks progress.
```
