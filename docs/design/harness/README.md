# Design harness

The desktop UI in a plain browser, with the Rust core faked, so every screen and state can be
looked at and screenshotted without Tauri, the API, a sign-in, or any model or STT spend.

- `mock.ts` fakes the core's commands and events with Tauri's `mockIPC`, then loads the app's own
  `apps/desktop/src/main.tsx`, unchanged. The URL picks the state (see the comment at its top):
  `?data=empty`, `?data=loading`, `?data=error`, `?data=long`, `?call=midcall`, `#overlay`, …
- `fixtures.ts` is the fictional data: no real people, companies or accounts.
- `shoot.mjs` screenshots every screen and state into a folder and writes `metrics.json`
  (page heights, the overlay's type sizes, how far the overlay's content moves while an answer
  streams).

It lives under `docs/`, so CI treats it as documentation, and borrows the desktop app's
`node_modules`.

```sh
pnpm install                                   # once, for apps/desktop's node_modules
apps/desktop/node_modules/.bin/vite --config docs/design/harness/vite.config.mjs
# open http://127.0.0.1:1430/?call=midcall#overlay, or screenshot everything:
npm i --prefix /some/dir playwright-core      # once; drives the installed Edge (or PW_CHANNEL=chrome)
PW_DIR=/some/dir node docs/design/harness/shoot.mjs docs/design/screens/before
```

Main-window shots are 1080×780 (the window's default size), overlay shots 760×620 (its default),
over a busy stand-in for a video call, since the overlay window is transparent.
