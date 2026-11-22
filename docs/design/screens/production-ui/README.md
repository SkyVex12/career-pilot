# Production UI validation

This review uses the real desktop React screens in installed Edge with fictional native IPC,
profiles, calendars, calls, and background-detection statuses. It does not read a microphone,
run ADB, contact a live account, or invoke a model. External requests are blocked and counted.

The curated `review/` directory contains representative final screenshots and the full final
metrics. Raw captures remain local in ignored directories and can be regenerated with the
scripts in [the design harness](../../harness/README.md).

Native Android screenshots and the development-signed preview receipt are in the [mobile review](mobile/README.md).

The `before/`, `after/`, `intermediate/`, and preflight directories from this task are
intermediate implementation evidence, **not** a comparison against the original branch.
In particular, `before/session-start.png` captured a temporary Vite import error and is not
valid UI evidence.

## Final result — 2026-11-22

The main-window matrix corresponds to desktop product revision `6509af8`.
The latest result for each of **232 unique
screen/viewport/theme states** passes, with no axe violations, page errors, external requests,
horizontal overflow, clipped horizontal controls, nested page-scroll chains, or unreachable
Call footer. [Matrix metrics](review/matrix-metrics.json) retain the source run for each state.
The initial full matrix was followed by focused checks after scroll and calendar fixes.
Two development-harness reload/import interruptions were rechecked successfully in isolation.

| Check | Final result |
| --- | --- |
| Default 920×720 Call setup, disclosures closed | Entire setup and action footer fit; no page scrollbar |
| Smaller Call windows, including 400×240 | One page scroll area; action footer stays visible |
| Server/update notifications | 50 lifecycle states across 10 viewport/theme combinations pass |
| Stacked notification actions | All 60 close/retry/install reachability checks pass, including 400×240 |
| Notification layout and focus | Page bounds stay unchanged, new notices do not steal focus, Start call remains uncovered |
| Keyboard flows | Both themes pass Quick settings Escape, 24-language selection, modal focus trap/restoration, and PageDown in Past calls at 400×240 |
| Enlarged layout | Both themes pass the 200% CSS zoom simulation without horizontally clipped controls |
| Independent live overlay | All 4 theme/size combinations pass axe, runtime, and horizontal-layout checks after removing dimmed enabled controls |

The [notification metrics](review/notification-metrics.json) and
[keyboard metrics](review/interaction-metrics.json) contain the measured bounds and outcomes.
The [overlay smoke metrics](review/overlay-metrics.json) cover 760×620 and 400×520 in both themes.
Visual review of the selected screens found aligned provider cards, readable narrow layouts,
consistent surfaces in both themes, and floating notices separated from page layout.

Representative screenshots:

- [Call, default dark](review/call-start-920-dark.png), [Call, large light](review/call-start-1080-light.png), and [Call, native minimum](review/call-start-400-light.png).
- [Floating notices](review/notices-920-dark.png) and [bounded notices at native minimum](review/notices-400-dark.png).
- [Calendar reconnection](review/calendar-reauth-920-light.png), [Settings](review/settings-920-dark.png), and [narrow Background settings](review/settings-background-390-light.png).
- [Ask with source](review/ask-answer-1080-light.png), [active Practice](review/practice-active-1080-light.png), and [narrow Practice setup](review/practice-390-light.png).
- [Independent live overlay, light](review/overlay-760-light.png), with enabled Copy controls kept at full contrast.

## Coverage

- Call setup, language selection, calendar connection states, and the live call screen.
- General, Background/Hushed, Audio, Account, and Shortcuts settings.
- Interview prep, Work context, Past calls, Ask and its answer view, Practice and its active
  session, and Personal AI context. Empty, loading, error, and long-data states are included.
- Light and dark themes at 1080×780, the native default 920×720, 720×520, 390×780, and the
  native minimum 400×240. The 390-pixel browser viewport is an additional layout stress case;
  it is narrower than the native desktop minimum.
- Axe WCAG A/AA checks, runtime errors, external requests, horizontal clipping, nested page
  scrolling, and reachability of the Call footer. Textareas, listbox popups, dialogs, and the
  independent notification stack are excluded from page-scroll-chain checks.
- Server and app-update notices remain fixed at the lower right without changing page
  dimensions. At 400×240, the independent notice stack scrolls within its own bounded area,
  leaving the Call action footer accessible.
- Keyboard opening and Escape dismissal of Quick settings, all 24 language options,
  application-dialog focus trapping/restoration, keyboard scrolling at minimum size, and a
  200% CSS zoom layout simulation.

## Limits

These checks validate browser-rendered layout and fixture interactions. They do not certify
native WebView zoom, OS notification delivery, actual screen-capture exclusion, or real
audio/calendar/network behavior. Native window fixes and device testing are recorded
separately in the implementation ledger. Automated axe checks complement visual and keyboard
review; they do not prove complete accessibility compliance.
