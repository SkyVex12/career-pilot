# Material 04: Mobile companion for iOS and Android

Status: Agreed feature saved for future implementation. The implementation details below are proposed requirements; saving this material does not start implementation.

Purpose: Let users keep a call and headphones on their computer while reading and controlling live assistance on their phone. Extend the same account to mobile Ask, practice, and saved-session review.

Primary product benefit: Keep your headphones on. Get live guidance on your phone.

## Implementation prompt

```text
Implement Career Pilot Mobile for iOS and Android, prioritizing a companion to the existing Windows and macOS desktop app.

Repository: C:\Users\A\WorkSpace\career-pilot

Inspect repository instructions, authentication, live-session ownership, event delivery, audio capture, guest permissions, and existing frontend/backend architecture first. Preserve user changes. Reuse the existing production backend and authoritative account, profile, application, and session data rather than creating a separate mobile service or duplicate knowledge store.

Select the mobile framework and supported OS versions based on current official platform requirements and the existing codebase. Deliver installable iOS and Android apps; a browser prototype alone is not completion. Preserve desktop support for Windows and macOS 13+ on Apple Silicon and Intel. A browser extension is not required.

1. Headphone-compatible desktop companion: first priority

Support this explicit flow:

Computer call audio and microphone -> existing desktop capture and backend session -> live questions and assistance on the phone.

- The meeting stays on the computer. Headphones and microphone stay connected to the computer.
- The desktop captures the selected call audio and microphone using its existing supported capture paths.
- The phone displays session events and answers; it does not need to hear room audio or request microphone access for this mode.
- Connecting the phone attaches to the existing session. Do not start a second transcription pipeline, generate duplicate answers, or bill a duplicate session merely because another display is connected.
- Make the audio-source computer and connection state visible on the phone.
- Let the user choose to use the phone as the guidance display while the desktop overlay is hidden. Preserve a way to reopen the overlay or main app.
- Keep the shared screen free of phone UI unless the user deliberately mirrors it. Describe screen-share behavior through tested configurations rather than promising universal undetectability.

Do not quietly fall back to the phone microphone when the desktop disconnects. Explain the interruption and offer an explicit supported alternative.

2. Account connection and session access

Use the existing account and profile system. Provide a simple way to open the current desktop session on the phone, such as same-account sign-in followed by a visible active-session card.

If QR pairing improves the flow, use a short-lived, single-use pairing exchange with confirmation and revocation. Do not place reusable account credentials or long-lived bearer tokens in QR codes or URLs.

- Enforce account, profile, session, and command permissions on the backend.
- Clearly identify the session and selected profile before attaching.
- Treat attaching a companion as a separate action from taking over audio capture on another device.
- Keep a single audio-capture owner until the user deliberately transfers or ends it.
- Reuse existing session-owner and guest-role semantics where appropriate without granting broader permissions than a companion needs.
- Disconnect a paired device safely on sign-out or revocation.
- Do not record audio merely because a user opens a pairing link or signs in.

3. A phone interface built for live use

Design for a small touch screen rather than shrinking the desktop window.

- Keep the current question and its answer together in one clear view.
- Support portrait and landscape, readable text sizes, dark/light themes, and accessible touch targets.
- Let users review previous answers without new content forcibly moving their reading position. Provide a clear return-to-latest action.
- Provide compact Answer, ask-a-follow-up, and End call controls.
- End call must use the existing confirmation and post-call choices, operate on the same session, and restore the normal desktop page through the established lifecycle.
- Keep presentation preferences such as text size local to the device unless the user deliberately synchronizes them.
- Offer keeping the screen awake during an active companion session; release that state when the session ends or the user leaves it.
- Avoid presenting detailed private answer text in lock-screen notifications by default.

Preserve the current ruby branding and established icons. Avoid extra menu sections, redundant status labels, and multiple competing question boxes.

4. Reliable synchronization and remote actions

Stream updates from the authoritative session with ordering, stable event identifiers, and reconnect recovery.

- Replay missed events or load a current snapshot after reconnecting without duplicating transcript lines or answers.
- Distinguish connected, reconnecting, ended, and unavailable states; do not present stale text as a newly received live answer.
- Preserve the phone's draft and reading position during connection changes.
- Handle Wi-Fi/cellular switching, device sleep, app backgrounding, and returning to the foreground.
- A phone disconnect must not end the desktop call or lose its ongoing capture.
- Respect mobile background limits instead of assuming an indefinitely active socket while suspended.
- Make remote commands idempotent where appropriate and acknowledge their actual result.
- Resolve simultaneous desktop and phone actions predictably, especially Answer and End call.
- Authorize every remote command for the current user and session.

Sending session text and control events is sufficient for the main companion mode. Do not stream raw meeting audio to the phone unless a separately justified feature needs it.

5. Standalone phone microphone mode

As a separate mode, support a user-started mobile microphone session for an in-person conversation or a call that is audible from another device's speaker.

- Explain the selected audio source before starting.
- Request microphone permission only for a feature that uses it.
- Show recording/listening state and offer immediate pause and stop actions.
- Preserve the existing transcript-consent, retention, and deletion behavior.
- Handle noise, microphone interruption, permission loss, headphones, and incoming calls honestly.
- If the remote speaker is audible only through headphones on another device, explain that the phone microphone cannot hear them and offer desktop companion mode when available.
- Use platform-supported background recording only after an explicit user start, with the platform's required indicators and lifecycle handling.

Do not promise universal capture of cellular, FaceTime, WhatsApp, Google Meet, or other calls running on the same phone. Treat those as separate app/OS-specific capabilities that require verified API support and real-device testing. Background microphone permission does not establish access to another app's call audio.

Do not bypass operating-system permissions or imply that a mobile app can hide every indicator or detection signal. Test and describe the supported screen-sharing and audio behavior precisely.

6. Central Ask, practice, and existing information

Extend the mobile app with these shared experiences as their backend capabilities become available:

- Central Ask from Material 03, including context scope, saved conversations, grounded answers, and source links.
- Spoken practice interviews from Material 02, including turn controls, feedback, retries, and progress.
- Existing applications, resumes, and saved call summaries/transcripts for preparation and review.
- Calendar-based preparation from Material 01 when a calendar is connected.

Keep account/profile isolation and the same data lifecycle across devices. Opening a source on mobile should preserve the Ask conversation. Practice must keep its generated interviewer audio separate from the user's captured speech, as specified in Material 02.

These integrations must not block delivery of the first desktop-companion release. Do not add placeholder controls that look functional for unavailable features.

7. Data, battery, and production behavior

- Store authentication secrets using platform-appropriate secure storage and use encrypted authenticated transport.
- Revalidate access when loading or reconnecting to a session.
- Stop unused capture, wake locks, event subscriptions, and generation tasks promptly.
- Keep idle and background behavior lightweight; do not wake production services unnecessarily.
- Define cache and offline behavior explicitly. Cached answers may be readable when allowed, but generating a new live answer must not appear to work without the necessary connection.
- Apply deletion and access changes to mobile caches and source views consistently with the existing backend lifecycle.
- Keep model/provider secrets on the backend.
- Provide production configuration and install instructions using the actual release channels. Report any signing or store-account requirements that need the user to complete an external step.

8. Delivery order

First deliver and validate desktop audio capture with phone display and controls on iOS and Android. Then add the standalone microphone mode and integrate Central Ask, practice, and preparation as those capabilities become ready.

The first release must demonstrate the central benefit with headphones on the computer and no microphone permission on the phone. Do not substitute a room-listening-only implementation for that requirement.

9. Validation and deliverables

Add meaningful automated tests for pairing, authorization, profile isolation, attachment versus capture takeover, event ordering/replay, command deduplication, cancellation, session end, reconnect recovery, and cleanup.

Validate the main workflow on actual devices, including:

- Windows and macOS desktops paired with both iPhone and Android.
- Supported wired, USB, and Bluetooth headphone/microphone configurations, including device changes.
- Meeting audio from supported desktop apps and browser calls.
- Operation while phone microphone permission is denied in companion mode.
- Simultaneous desktop/phone actions and multiple connected displays.
- Phone lock/unlock, background/foreground, Wi-Fi/cellular changes, desktop sleep, and network loss.
- Clear handling of an ended or deleted session and sign-out/revocation.
- End call confirmation and restoration of the normal desktop page.
- Standalone phone microphone interruptions and unavailable same-phone call audio.
- Portrait/landscape layout, large text, keyboard, and assistive-technology behavior.

Distinguish simulated tests, native builds, and real hardware results. Use deterministic fixtures for routine tests instead of paid model calls. Keep CI efficient, cache builds appropriately, and reserve expensive packaging/signing work for relevant changes or release workflows.

Finish with the working mobile apps, desktop/backend integration, validation results, concise setup and release instructions, and any remaining platform-specific limitations or external signing requirements.

Resolve routine implementation choices independently and ask only for missing information that blocks progress.
```

## Research supporting the scope

Reviewed on 2026-11-19. These are documented vendor/platform capabilities, not results of hands-on competitor testing.

- [LockedIn mobile](https://www.lockedinai.com/mobile-app) describes placing a phone beside a laptop to listen to the room. Its page reserves Stealth Mode for the desktop app.
- [OfferGoose help](https://blog.offergoose.com/post104/) documents mobile microphone listening, the speaker/headphone limitation, and the need for another device when taking a phone call.
- [Android audio-input sharing](https://developer.android.com/media/platform/sharing-audio-input) explains microphone contention and restrictions on call audio.
- [Android playback capture](https://developer.android.com/media/platform/av-capture) limits which playback streams can be captured and requires user authorization.
- [Apple recording audio sessions](https://developer.apple.com/documentation/avfaudio/avaudiosession/category-swift.struct/record) documents recording behavior and interruptions.

These findings support prioritizing desktop capture with a phone display. They do not establish that every competitor lacks a synchronized companion mode or that any product is universally undetectable.
