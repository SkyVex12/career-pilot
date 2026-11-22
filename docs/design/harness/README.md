# Design harness

The desktop UI in a plain browser, with the Rust core faked, so every screen and state can be
looked at and screenshotted without Tauri, the API, a sign-in, or any model or STT spend.

- `mock.ts` fakes the core's commands and events with Tauri's `mockIPC`, then loads the app's own
  `apps/desktop/src/main.tsx`, unchanged. The URL picks the state (see the comment at its top):
  `?data=empty`, `?data=loading`, `?data=error`, `?data=long`, `?call=midcall`, `#overlay`, …
- `fixtures.ts` is the fictional data: no real people, companies or accounts.
- `production-ui.mjs` screenshots the current screens and states into a folder and writes `metrics.json`
  (page heights, the overlay's type sizes, how far the overlay's content moves while an answer
  streams).

It lives under `docs/`, so CI treats it as documentation, and borrows the desktop app's
`node_modules`.

```sh
pnpm install                                   # once, for apps/desktop's node_modules
apps/desktop/node_modules/.bin/vite --config docs/design/harness/vite.config.mjs
# open http://127.0.0.1:1430/?call=midcall#overlay, or screenshot everything:
npm i --prefix /some/dir playwright-core      # once; drives the installed Edge (or PW_CHANNEL=chrome)
PW_DIR=/some/dir node docs/design/harness/production-ui.mjs docs/design/screens/production-ui/final
```

Main-window checks include the native default 920×720 and larger 1080×780 windows.
The legacy `shoot.mjs` also has 760×620 overlay scenarios over a busy stand-in for a video
call, since the overlay window is transparent; its older navigation selectors may need
updating when checking those historical scenarios.

## Sharing and Windows regression scenarios

Use the real React screens with local media fixtures:

- `?call=assisting&screen=waiting`, `on`, `control`, `error`, or `denied` shows the assistant workspace states.
- Add `#overlay` and open Shared screen to inspect the receiver in the overlay.
- `?call=midcall&assistants=1#overlay` shows an admitted assistant, an invitation, access selectors, and the screen-source picker.
- `?call=ended` must open Start call, without requesting the previous review.
- `?call=midcall&summary=empty`, then `window.__harness.end()`, shows the unavailable-summary state.

`assistance.ts` creates a labeled canvas desktop and mock room. It never reads a real screen,
opens a microphone, connects to LiveKit, or sends remote input. These fixtures validate
rendering and interaction only; native resizing, actual capture visibility and two-device
media delivery require the desktop build.

## Production UI review

`production-ui.mjs` audits the current navigation, call setup and live view, all Settings
sections, calendar states, the 24-language picker, Interview prep, Work context, Ask,
Practice, and Past calls. It captures 1080×780, the default 920×720, 720×520, 390×780, and the
native minimum 400×240, in light and dark themes. Each state runs axe WCAG A/AA checks
and records horizontal overflow, clipped controls, nested page scroll chains, call-footer
reachability, page errors, and blocked external requests in `metrics.json`.

```sh
PW_DIR=/folder/containing/node_modules node docs/design/harness/production-ui.mjs docs/design/screens/production-ui/after
PW_DIR=/folder/containing/node_modules node docs/design/harness/production-interactions.mjs docs/design/screens/production-ui/interactions
PW_DIR=/folder/containing/node_modules node docs/design/harness/production-notifications.mjs docs/design/screens/production-ui/notifications
PW_DIR=/folder/containing/node_modules node docs/design/harness/production-overlay.mjs docs/design/screens/production-ui/overlay-final
```

The screenshot script accepts a comma-separated scene filter as its second argument and
runs two isolated pages at a time (`UI_WORKERS=1` runs sequentially).
Keep product source stable during an audit; Vite hot reload can invalidate the fixture's
native-event callbacks. Rerun any state interrupted by a source reload.
`UI_SIZES=1080x780,400x240` and `UI_SCHEMES=light,dark` restrict the matrix. `PW_CHANNEL` defaults
to installed Edge. The interaction script verifies keyboard popover dismissal, all 24
language options, modal focus trapping/restoration, and a 200% CSS zoom layout simulation.
That simulation is not certification of native WebView/browser zoom.
The notification checks emit fictional server/update events and verify fixed positioning,
unchanged page dimensions, dismissal across progress updates, and visibility of the call footer.
Every stacked notice action is also scrolled into view, focused, and hit-tested at each size.
The standalone overlay smoke audits live-call rendering in both themes at 760×620 and 400×520,
including accessible contrast after the fictional live session has finished initializing.

`production.ts` provides fictional background/Hushed statuses. `?calendar=connected`
adds one fictional meeting and connected Google Calendar. `languages.json` mirrors the
supported language catalog for rendering only. `?calendar=reauth` shows an expired calendar
authorization with cached meeting data. These fixtures never execute ADB, access
real calendars, start audio, or contact a production server. Native OS detection and
notifications require separate validation.
