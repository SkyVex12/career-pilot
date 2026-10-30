// Screenshots every screen and state of the desktop UI in the design harness (README.md), and
// measures a few things the audit quotes: page heights, where the Start button sits, the
// overlay's type sizes, and how much the overlay's layout moves while an answer streams.
//
//   node docs/design/harness/shoot.mjs <out dir> [only-this-prefix]
//
// Needs the harness running (vite, port 1430) and playwright-core; PW_DIR names the folder
// holding its node_modules. It drives the Edge or Chrome already installed.
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const require = createRequire(join(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");

const out = process.argv[2] ?? "docs/design/screens/before";
const only = process.argv[3] ?? "";
mkdirSync(out, { recursive: true });
const BASE = "http://127.0.0.1:1430/";
const MAIN = { width: 1080, height: 780 };
const OVERLAY = { width: 760, height: 620 };

// What sits behind the transparent overlay: a busy stand-in for a video call.
const BEHIND = `html{background:
  repeating-linear-gradient(90deg,#3a5f7d 0 180px,#c9b28f 180px 360px,#2e3b2f 360px 520px,#e8e2d6 520px 760px) !important}
  html::before{content:"Video call: shared screen with a spreadsheet, code and faces";position:fixed;inset:0;
  font:600 22px/1.8 Segoe UI;color:#111;padding:40px;white-space:pre-wrap;word-spacing:30px}`;

const tab = (label) => async (p) => {
  await p.locator(`nav.tabs button, nav[aria-label="Main"] button`, { hasText: label }).first().click();
  await p.waitForTimeout(400);
};
const click = (text, opts = {}) => async (p) => {
  await p.getByRole("button", { name: text, exact: opts.exact ?? false }).first().click();
  await p.waitForTimeout(400);
};
const seq = (...fs) => async (p) => {
  for (const f of fs) await f(p);
};

const main = [
  ["session-start", "?", null],
  ["session-start-standup", "?", click("standup", { exact: true })],
  ["session-start-meeting", "?", click("meeting", { exact: true })],
  ["session-start-empty", "?data=empty", null],
  ["session-start-loading", "?data=loading", null],
  ["session-start-error", "?data=error", null],
  ["session-signed-out", "?data=signedout", null],
  ["session-locked", "?data=locked", null],
  ["session-running", "?call=midcall", null],
  ["session-end-choice", "?call=midcall", click("End session")],
  ["session-postcall", "?call=ended", null],
  ["session-other-active", "?call=session_active", null],
  ["session-update-ready", "?update=ready", null],
  ["applications", "?", tab("Applications")],
  ["applications-empty", "?data=empty", tab("Applications")],
  ["applications-loading", "?data=loading", tab("Applications")],
  ["applications-error", "?data=error", tab("Applications")],
  ["applications-long", "?data=long", tab("Applications")],
  ["applications-editor", "?", seq(tab("Applications"), async (p) => (await p.locator("table.list tr").first().click(), p.waitForTimeout(500)))],
  ["applications-new", "?", seq(tab("Applications"), click("Add application"))],
  ["applications-pull", "?", seq(tab("Applications"), click("Pull from Career-OS"))],
  ["resumes", "?", tab("Resumes")],
  ["resumes-empty", "?data=empty", tab("Resumes")],
  ["resumes-loading", "?data=loading", tab("Resumes")],
  ["resumes-error", "?data=error", tab("Resumes")],
  ["resumes-long", "?data=long", tab("Resumes")],
  ["work-log", "?", tab("Work")],
  ["work-material", "?", seq(tab("Work"), click("Material"))],
  ["work-facts", "?", seq(tab("Work"), click("Facts", { exact: true }))],
  ["work-delete-company", "?", seq(tab("Work"), click("Delete all data"))],
  ["work-empty", "?data=empty", tab("Work")],
  ["work-loading", "?data=loading", tab("Work")],
  ["work-error", "?data=error", tab("Work")],
  ["work-long", "?data=long", tab("Work")],
  ["history", "?", tab("History")],
  ["history-empty", "?data=empty", tab("History")],
  ["history-loading", "?data=loading", tab("History")],
  ["history-error", "?data=error", tab("History")],
  ["history-long", "?data=long", tab("History")],
  ["history-session", "?", seq(tab("History"), async (p) => (await p.locator("table.list.clickable tr, .ui-list .ui-row-hit").first().click(), p.waitForTimeout(800)))],
  ["settings", "?", tab("Settings")],
  ["settings-dev-build", "?dev=1", tab("Settings")],
  ["settings-signed-out", "?data=signedout", tab("Settings")],
  ["settings-locked", "?data=locked", tab("Settings")],
  ["debug", "?call=midcall", async (p) => (await p.keyboard.press("Control+Shift+D"), p.waitForTimeout(500))],
];

const overlay = [
  ["overlay-idle", "?", OVERLAY, null],
  ["overlay-midcall", "?call=midcall", OVERLAY, null],
  ["overlay-midcall-stacked", "?call=midcall&layout=stacked", OVERLAY, null],
  ["overlay-midcall-compact", "?call=midcall&compact=1", OVERLAY, null],
  ["overlay-midcall-compact-small", "?call=midcall&compact=1", { width: 420, height: 480 }, null],
  ["overlay-midcall-narrow", "?call=midcall", { width: 380, height: 600 }, null],
  ["overlay-opacity-60", "?call=midcall&opacity=0.6", OVERLAY, null],
  ["overlay-opacity-35", "?call=midcall&opacity=0.35", OVERLAY, null],
  ["overlay-text-130", "?call=midcall&scale=1.3", OVERLAY, null],
  ["overlay-click-through", "?call=midcall&ct=1", OVERLAY, null],
  ["overlay-menu", "?call=midcall", OVERLAY, async (p) => (await p.getByTitle("Overlay settings").click(), p.waitForTimeout(300))],
  ["overlay-degraded", "?call=degraded", OVERLAY, null],
  ["overlay-reconnecting", "?call=reconnecting", OVERLAY, null],
];

const metrics = {};
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge" });

for (const scheme of ["light", "dark"]) {
  for (const [name, query, act] of main) {
    const file = `${name}${scheme === "dark" ? "-dark" : ""}`;
    if (!file.startsWith(only)) continue;
    // Dark only for the screens the audit compares side by side.
    if (scheme === "dark" && !["session-start", "applications", "work-log", "history-session", "settings"].includes(name)) continue;
    const page = await browser.newPage({ viewport: MAIN, colorScheme: scheme });
    await page.goto(BASE + query);
    await page.waitForTimeout(700);
    if (act) await act(page);
    await page.waitForTimeout(300);
    await page.screenshot({ path: join(out, `${file}.png`) });
    if (scheme === "light") {
      metrics[name] = await page.evaluate(() => {
        const start = [...document.querySelectorAll("button")].find((b) => /^Start session$/.test(b.textContent ?? ""));
        return {
          pageHeight: document.documentElement.scrollHeight,
          startButtonY: start ? Math.round(start.getBoundingClientRect().top) : null,
          buttons: document.querySelectorAll("button").length,
          notes: document.querySelectorAll(".note").length,
        };
      });
      if (["applications-long", "history-long", "session-start", "settings", "work-log", "history-session"].includes(name)) {
        await page.screenshot({ path: join(out, `${file}-full.png`), fullPage: true });
      }
    }
    await page.close();
  }
}

for (const [name, query, viewport, act] of overlay) {
  if (!name.startsWith(only)) continue;
  const page = await browser.newPage({ viewport, colorScheme: "dark" });
  await page.goto(`${BASE}${query}#overlay`);
  await page.addStyleTag({ content: BEHIND });
  await page.waitForTimeout(700);
  if (act) await act(page);
  await page.screenshot({ path: join(out, `${name}.png`) });
  metrics[name] = await page.evaluate(() => {
    const px = (sel) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el).fontSize : null;
    };
    const cards = document.querySelectorAll(".card");
    const last = cards[cards.length - 1];
    const r = last?.querySelector(".points")?.getBoundingClientRect();
    return {
      fastAnswer: px(".points .bullets"),
      deepAnswer: px(".deep .bullets"),
      question: px(".card .question"),
      transcript: px(".tx .line"),
      passTitle: px(".pass-title"),
      latestFastVisible: r ? r.top >= 0 && r.top < innerHeight : null,
      latestFastTop: r ? Math.round(r.top) : null,
    };
  });
  await page.close();
}

// Streaming: a new question's answers arrive word by word. Frames along the way, and how far
// the content moved (layout-shift score and the fast answer's top edge per frame).
if ("overlay-stream".startsWith(only) || only === "") {
  const page = await browser.newPage({ viewport: OVERLAY, colorScheme: "dark" });
  await page.goto(`${BASE}?call=stream#overlay`);
  await page.addStyleTag({ content: BEHIND });
  await page.waitForTimeout(800);
  await page.evaluate(() => {
    window.__cls = 0;
    window.__tops = [];
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__cls += e.value;
    }).observe({ type: "layout-shift", buffered: false });
    const tick = () => {
      const q4 = document.querySelector('[data-q="q4"] .points');
      if (q4) window.__tops.push(Math.round(q4.getBoundingClientRect().top));
      requestAnimationFrame(tick);
    };
    tick();
    window.__done = window.__harness.play(45);
  });
  for (const [i, ms] of [[1, 300], [2, 1200], [3, 2600], [4, 4500]]) {
    await page.waitForTimeout(i === 1 ? ms : ms - [0, 300, 1200, 2600][i - 1]);
    await page.screenshot({ path: join(out, `overlay-stream-${i}.png`) });
  }
  await page.evaluate(() => window.__done);
  await page.waitForTimeout(300);
  await page.screenshot({ path: join(out, "overlay-stream-5-done.png") });
  metrics["overlay-stream"] = await page.evaluate(() => {
    const t = window.__tops;
    let jumps = 0;
    for (let i = 1; i < t.length; i++) if (Math.abs(t[i] - t[i - 1]) > 2) jumps++;
    return { cls: Number(window.__cls.toFixed(3)), fastTopMin: Math.min(...t), fastTopMax: Math.max(...t), framesThatMoved: jumps, frames: t.length };
  });
  await page.close();
}

await browser.close();
writeFileSync(join(out, "metrics.json"), JSON.stringify(metrics, null, 2));
console.log(JSON.stringify(metrics, null, 2));
