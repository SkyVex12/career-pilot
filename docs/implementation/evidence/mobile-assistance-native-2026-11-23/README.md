# Android native acceptance — 2026-11-23

The isolated Android test app passed the native UI and API journeys listed below. This is **not a certification of live screen, remote control, or voice transport**: the local LiveKit fixture never completed ICE. The app recovered from that failed connection and remained usable.

The authoritative build, source hashes, limits, health counts, cleanup and image hashes are in [the acceptance receipt](../mobile-assistance-native-2026-11-23.json). This package uses `app.careerpilot.mobile.assistantsmoke`, separate from the user's installed app. All accounts, requests, calls and documents shown here are fictional. No real microphone audio, OAuth account, call, private content or desktop-control input was used.

| Journey | Native observation | Evidence |
| --- | --- | --- |
| Assist another call | Invitation, acceptance, authorized join, live guidance/transcript | `assistant-invitation.png`, `assistant-guidance.png` |
| Private note | Delivered acknowledgement; one server command persisted across foreground reconnect | `assistant-note-delivered.png`, `native-fixture-receipts.json` |
| Sharing and revocation | Paused sharing cleared guidance; revoked seat cleared content and removed controls | `assistant-sharing-paused.png`, `assistant-revoked.png` |
| Screen failure recovery | Screen unavailable with Reconnect; no decoded frames or live control claimed | `assistant-screen-unavailable.png` |
| Work context | Work log and pasted document saved | `workspace-log-saved.png`, `workspace-document-saved.png` |
| Resume and interview | Tailored resume saved; scheduled interview handed off to For interviews with selected application and microphone off | `applications-resume-saved.png`, `scheduled-interview-context.png` |
| Meeting preparation | Edited unfinished action saved | `calendar-meeting-prep.png`, `native-fixture-receipts.json` |
| Ask | Response completed; cited source opened with matching transcript/time | `ask-answer-native.png`, `ask-source-native.png` |
| Personal AI | Request created, pasted brief reviewed, one fact selected and imported; saved fact corrected, another excluded; revision preserved saved content | `personal-ai-review.png`, `personal-ai-imported.png`, `personal-ai-correction.png`, `personal-ai-exclusion.png` |
| AI client management | Confirmed disconnect removed fictional client, retained briefs | `personal-ai-client-disconnected.png`, `personal-ai-native-receipts.json` |
| HubRuby | Fictional connection, search, destination confirmation, application/JD/resume import, refresh and disconnect | `hubruby-imported.png`, `hubruby-refreshed.png`, `hubruby-native-receipts.json` |
| Cleanup and stability | Signed out; zero retained app crash/ANR/fatal/unhandled matches | `signed-out-cleanup.png`, `app-health.json` |

The API fixtures are stateful test implementations. Their extraction, indexing, model answers, calendar and HubRuby data are synthetic. Production API behavior is validated separately by backend tests. The raw `media_observed.screenConnected` and `voiceConnected` fields are signaling flags only and must not be interpreted as delivered media.

The bounded ICE investigation observed more than 150 valid STUN requests traversing the Android/ADB/loopback relay without any reply from the local media endpoint. Neither relevant native nor browser publisher logs showed a nominated ICE pair. No further media retries were made. A working device/media-network acceptance run is still needed for readable decoded screen frames, resolution changes, host approval and remote input, and live voice.

The test account was signed out, all owned API/media/relay/browser processes and ADB reverse mappings were removed, and the isolated app was left installed at sign-in. Its microphone permission remains denied; the package app-op was returned to default. The user's installed app and data were not changed.
