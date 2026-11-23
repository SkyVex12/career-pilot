# Mobile and desktop assistance follow-up

Started 2026-11-23 on `codex/mobile-assistance-parity` from clean `1e34389`. This extends the original six-material scope after the user's APK testing. Original requirement IDs and historical validation remain in the main ledger.

| ID | User journey and acceptance | Status |
| --- | --- | --- |
| PAR-001 | Mobile discovers invitations, accepts a guest seat and joins another user's live call without owning or duplicating capture. Permissions, revoked seats, reconnect, foreground and account/profile cleanup are enforced. | Implemented; native acceptance in progress |
| PAR-002 | Mobile assistant receives authorized transcript/answers, sends private notes and talks to the host using existing LiveKit infrastructure. | Implemented; native acceptance in progress |
| PAR-003 | Mobile and desktop assistants view shared screens with selectable actual stream resolution, readable zoom and visible connection/quality state. Native capture must supply the selected detail. | Implemented; native acceptance in progress |
| PAR-004 | A viewing assistant can request control; the host sees who requested it and approves or declines. Input is rejected before approval and released on revoke, leave, timeout and share end. | Implemented; native acceptance in progress |
| PAR-005 | Desktop and mobile Ask submit, display accepted pending work, recover from failed reads and finish/retry/cancel scoped answers. Composer remains reachable at small sizes. | Verified with fixtures and real local API stack |
| PAR-006 | Audit mobile Work against desktop company/log/document/memory management; implement feasible missing account/profile-scoped journeys. | Implemented; API and client contracts verified, native acceptance in progress |
| PAR-007 | Audit interview preparation, applications/resumes, calendar and call-setup/host-assistant controls; finish feasible functional gaps without adding primary tabs. | Implemented; API and client contracts verified, native acceptance in progress |
| PAR-008 | Validate cross-client guest/host approval, quality selection, Ask recovery, background/reconnect and account isolation. Keep fixture, provider, emulator and physical-device evidence distinct. | Pending |
| PAR-009 | Build updated desktop and mobile previews; verify packages and preserve previous artifacts. Update ledger, deliver artifacts and record exact remaining external limitations. | Pending |
| PAR-010 | Terminal call information floats at the bottom right, is dismissible, expires after five seconds, and never replays on tab/profile changes. | Verified in rendered desktop scenes |
| PAR-011 | Simplify new and live call choices to For interviews / For work, preserving legacy saved data and wire compatibility. | Verified in desktop rendering and mobile compilation |
| PAR-012 | Mobile manages private Personal AI requests/briefs and existing HubRuby application imports through the same authorized APIs as desktop. | Implemented; API and client contracts verified, native acceptance in progress |

Implementation decisions:

- Reuse existing seat authorization, gateway events and LiveKit media. No new backend or paid provider.
- Keep four primary mobile tabs. Assistant invitations belong in Call; Ask remains in Work and practice remains in Interview prep.
- The user's request for desktop functionality means mobile versions of supported product journeys. Desktop OS integration such as Windows ADB detection and native window hotkeys requires a platform-specific equivalent or an explicit documented limitation; it must not be represented by a nonfunctional mobile control.
- The existing phone microphone mode remains explicit; connecting as an assistant never starts capture of another app's audio.
- Latest wording decision: **For interviews** and **For work**. The user preferred purpose labels after reviewing Interview / Work meeting and Interview / Meeting. Wire modes remain `interview` / `meeting`; historical standup/coding records remain readable.
- Success and terminal informational messages float outside scrolling pages and expire. Actionable errors and unavailable configuration remain beside the affected control.

## November 23 implementation checkpoint

- Mobile assistant adds account-authorized guest invitation/join, private notes, explicitly enabled voice, screen viewing, 720p–4K requests, zoom, request-correlated host consent and remote touch/key input. Native media uses the existing LiveKit service. Mobile host listening has a distinct receive-only `host:<device_id>` identity so it cannot evict the desktop host. Companion sharing changes require the owner's command and authoritative desktop acknowledgment.
- Desktop screen capture honors selected resolution rather than stretching a capped image. Main workspace exclusively owns remote-control consent: an exact session/request temporarily shows/focuses the window and accepts clicks, restoring click-through and always-on-top preferences afterward or at timeout. Canceled approvals, revoked permissions and old input-queue failures cannot grant/control a newer request.
- Ask publishes accepted pending messages before secondary reads, recovers polling without duplicate submissions, maintains context-scoped retry IDs and shows private-safe processing errors. Work Ask remains available while the employer listing is unavailable. Its composer is visible in small desktop windows.
- Mobile company management, log/attachment uploads, knowledge documents, memory edits/archival and proposal review use real existing APIs. Delayed refresh/day navigation preserve the latest unsaved log text, and clean saved snapshots are not retained as drafts. Application/resume CRUD, upload processing, scheduling and contextual practice/Ask routes are implemented; accepted save/delete/schedule receipts update local state before optional refresh. Calendar selection/sync/disconnect and Google/Outlook authorization availability are exposed inside Meeting preparation. HubRuby search/import/refresh requires source ownership and explicit destination confirmation. Personal AI request, publication review, corrections, exclusions, revision management and MCP client revocation live inside Work.

## Validation so far (current follow-up only)

- Desktop Ask/Call fixture checks: 9 Ask tests, 23 call design tests; 18 rendered light/dark scenes over three window sizes pass without axe findings, nested scrollbars, clipped controls or JS errors. Separate floating-toast checks verify fixed placement, no layout shift on dismissal and no replay.
- Screen/consent integration: 94 desktop tests across screen, native screen, overlay and seats; 13 real local-stack API seat tests; 3 Windows native window tests; 5 earlier Windows capture tests. macOS capture source is updated but actual macOS runtime behavior is not certified here.
- Mobile assistant: 14 focused lifecycle/permission/audio lease tests; Android and iOS Hermes exports with native media dependencies passed. The isolated Android smoke build is compiling; it will not replace the user's installed app or use real accounts.
- Backend companion/API seats: 29 combined checks passed, including owner-only sharing acknowledgment and distinct mobile host voice identity. Calendar policy and real local stack: 9 passed. Work/application/original-resume real API stack: 17 passed.
- Mobile data/client contracts: 14 application tests; 16 calendar/history tests; 8 new work/upload tests cover strict parsing, dates, raw bytes, Unicode filenames, token refresh, profile pinning, account changes and abort-before-upload. Fictional native workspace/base fixture: 14 passed. Full integration suites are running; these focused counts are not represented as final full-suite totals.
- All provider/device limitations remain explicit. Fixture success does not establish real calendar OAuth, physical device microphone quality or production deployment. No paid provider or production infrastructure was added.

## Integrated source validation checkpoint

- Full backend suite: **2,605 passed, 1 skipped** against the local Postgres/Valkey/S3/queue stack. Python types: zero errors/warnings; Ruff check and format: all 274 files clean; generated protocol types current.
- Full desktop frontend: **600 tests across 57 files passed**, plus **10 release checks**; TypeScript and production Vite bundle pass. Rendered call-ended feedback stays fixed outside the scrolling page, dismisses without layout movement and does not replay. The existing large-bundle advisory remains.
- Native Windows: workspace formatting and strict Clippy (default and desktop release features) pass; **186 native desktop tests passed, 6 existing ignored**. Final executable compilation is in progress.
- Frozen mobile source: TypeScript and **156 tests passed**, none skipped. Additional native fixture tests cover fictional work/application/calendar/HubRuby services; provider API tests use fictional or local-stack providers. Android/iOS JS export and native compilation are tracked separately below when complete.
- Device limits: private assistant audio on an in-person host is available while its microphone capture is paused, preventing playback from entering the captured room audio; desktop-companion host listening is receive-only and independent. Remote text input currently supports explicit US physical-key mapping; unsupported characters receive a clear validation error. Phone OS-level capture of other apps, Windows ADB detection/hotkeys, physical-device audio quality, real provider OAuth and actual macOS capture require separate platform/provider evidence.

Initial findings (resolved in the implementation above):

- Mobile originally only attached to the signed-in owner's desktop session and lacked a guest-seat invitation/join interface.
- Desktop capture originally capped resolution at 1920×1080 and allowed control from a capability grant without a fresh approval prompt.
- Both Ask clients originally waited for a follow-up read before displaying an accepted question, hiding pending work when that read failed.

Next action: finish the remaining mobile integrations, reconcile the broader test suites, exercise the isolated native app against fictional services and local media, then package revision-specific deliverables and create one focused draft PR.
