// Repeatable production UI checks against the real React screens and fictional IPC.
// Start Vite with vite.config.mjs, then:
// PW_DIR=<folder with node_modules> node docs/design/harness/production-ui.mjs <output> [scene filter]
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const require = createRequire(resolve(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");
const out = resolve(process.argv[2] ?? "docs/design/screens/production-ui/after");
const filter = process.argv[3] ?? "";
const sizes = process.env.UI_WIDTHS
  ? process.env.UI_WIDTHS.split(",").map(Number).map(width => ({ width, height: width === 720 ? 520 : width === 400 ? 240 : width === 920 ? 720 : 780 }))
  : (process.env.UI_SIZES ?? "1080x780,920x720,720x520,390x780,400x240").split(",").map(size => { const [width, height] = size.split("x").map(Number); return { width, height }; });
const schemes = (process.env.UI_SCHEMES ?? "light,dark").split(",");
const axe = readFileSync("apps/desktop/node_modules/axe-core/axe.min.js", "utf8");
const base = "http://127.0.0.1:1430/";
const nav = (name) => async p => p.getByRole("navigation", { name: "Main", exact: true }).getByRole("button", { name, exact: true }).click();
const settings = async p => {
  await p.getByRole("button", { name: "Quick settings", exact: true }).filter({ visible: true }).first().click();
  await p.getByRole("button", { name: "All settings…", exact: true }).click();
  await p.getByRole("navigation", { name: "Settings sections" }).waitFor();
};
const details = (text) => async p => {
  const summary = p.locator("summary").filter({ hasText: text }).first();
  await summary.click();
  await summary.scrollIntoViewIfNeeded();
};
const sequence = (...steps) => async p => { for (const step of steps) await step(p); };
const openSelect = label => async p => {
  const trigger = p.getByRole("combobox", { name: label, exact: true });
  await trigger.scrollIntoViewIfNeeded();
  // A popup closes when its page scrolls. Let the page settle before opening it.
  await p.waitForTimeout(100);
  await trigger.click();
};
const calendar = sequence(details("Meeting preparation"), async p => {
  await p.locator(".calendar-connections").waitFor();
  await p.locator(".calendar-connections").scrollIntoViewIfNeeded();
});
const ask = sequence(nav("Work context"), async p => p.getByRole("button", { name: "Ask a question", exact: true }).first().click());
const practice = sequence(nav("Interview prep"), async p => p.getByRole("button", { name: "Practice interview", exact: true }).first().click());
const scenes = [
  ["call-start", "", null],
  ["call-language", "", sequence(details("Customize"), openSelect("Language"))],
  ["call-calendar", "", calendar],
  ["call-calendar-connected", "?calendar=connected", calendar],
  ["call-calendar-reauth", "?calendar=reauth", calendar],
  ["call-error", "?data=error", null],
  ["call-empty", "?data=empty", null],
  ["call-long", "?data=long", null],
  ["call-running", "?call=midcall", null],
  ["settings", "", settings],
  ["settings-background", "", sequence(settings, async p => {
    const background = p.getByRole("navigation", { name: "Settings sections" }).getByRole("button", { name: "Background", exact: true });
    if (await background.count()) await background.click();
    await p.getByRole("button", { name: "Check detection" }).click();
  }, details("Hushed in LDPlayer"))],
  ["settings-audio", "", sequence(settings, async p => p.getByRole("button", { name: "Audio", exact: true }).click())],
  ["settings-account", "", sequence(settings, async p => p.getByRole("button", { name: "Account", exact: true }).click())],
  ["settings-shortcuts", "", sequence(settings, async p => p.getByRole("button", { name: "Shortcuts", exact: true }).click())],
  ["interview-prep", "", nav("Interview prep")],
  ["work-context", "", nav("Work context")],
  ["past-calls", "", nav("Past calls")],
  ["ask", "", ask],
  ["ask-answer", "", sequence(ask, openSelect("Saved conversations"), async p => {
    await p.getByRole("option", { name: "Prepare for Acme", exact: true }).click();
    await p.getByText("Which project best fits this role?", { exact: true }).waitFor();
  })],
  ["practice", "", practice],
  ["practice-active", "", sequence(practice, openSelect("Practice history"), async p => {
    await p.getByRole("option", { name: /Backend engineering/ }).click();
    await p.getByRole("heading", { name: /Tell me about/ }).waitFor();
  })],
  ["personal-ai", "", sequence(nav("Work context"), details("Context from my AI"))],
  ...["empty", "error", "loading", "long"].flatMap(state => [
    [`interview-prep-${state}`, `?data=${state}`, nav("Interview prep"), true],
    [`work-context-${state}`, `?data=${state}`, nav("Work context"), true],
    [`past-calls-${state}`, `?data=${state}`, nav("Past calls"), true],
  ]),
];
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge", headless: true });
const results = [];
const jobs = schemes.flatMap(scheme => sizes.flatMap(size => scenes.map(scene => ({ scheme, ...size, scene }))));
let next = 0;
try {
  const worker = async () => { while (next < jobs.length) {
    const { scheme, width, height, scene: [scene, query, act, single] } = jobs[next++];
    if (filter && !filter.split(",").includes(scene)) continue;
    if (single && (width !== sizes[0].width || scheme !== schemes[0])) continue;
    const id = `${scene}-${width}-${scheme}`;
    const page = await browser.newPage({ viewport: { width, height }, colorScheme: scheme, reducedMotion: "reduce" });
    page.setDefaultTimeout(8000);
    const errors = [], externalRequests = [];
    page.on("pageerror", error => errors.push(String(error)));
    // The fixture must never make a live-user or production request.
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === new URL(base).origin) return route.continue();
      externalRequests.push(route.request().url()); return route.abort();
    });
    try {
      await page.goto(base + query);
      await page.getByRole("navigation", { name: "Main", exact: true }).waitFor();
      await page.waitForTimeout(250);
      if (act) await act(page);
      await page.waitForTimeout(200);
      await page.addScriptTag({ content: axe });
      const audit = await page.evaluate(async () => {
        const result = await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] } });
        return result.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) }));
      });
      const layout = await page.evaluate(() => {
        const label = el => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}${typeof el.className === "string" && el.className ? `.${el.className.trim().split(/\s+/).join(".")}` : ""}`;
        const scrollers = [...document.querySelectorAll("body *")].filter(el => {
          if (!el.getClientRects().length || el.matches("textarea,input,[role=listbox],.ui-select-menu") || el.closest(".ui-toast-viewport,[role=dialog]")) return false;
          const style = getComputedStyle(el);
          return /auto|scroll/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 2 && el.clientHeight > 0;
        });
        const nestedScrollChains = scrollers.flatMap(el => scrollers.filter(parent => parent !== el && parent.contains(el)).map(parent => [label(parent), label(el)]));
        return {
        overflow: document.documentElement.scrollWidth > innerWidth,
        innerWidth, documentWidth: document.documentElement.scrollWidth,
        clippedControls: [...document.querySelectorAll("button,input,select,textarea,[role=combobox]")].filter(el => {
          if (!el.getClientRects().length) return false;
          const r = el.getBoundingClientRect();
          return r.left < -1 || r.right > innerWidth + 1;
        }).map(el => ({ name: el.getAttribute("aria-label") || el.textContent?.trim().slice(0,80), left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right })),
        fonts: Object.fromEntries([".shell-link", ".ui-btn", "h1", ".ui-field-hint"].map(sel => [sel, document.querySelector(sel) ? getComputedStyle(document.querySelector(sel)).fontSize : null])),
        scrollContainers: scrollers.map(el => ({ selector: label(el), clientHeight: el.clientHeight, scrollHeight: el.scrollHeight })),
        nestedScrollChains,
      }; });
      await page.screenshot({ path: resolve(out, `${id}.png`), fullPage: true });
      const callFooter = page.locator(".call-design-footer").filter({ visible: true }).last();
      if (scene.startsWith("call-") && await callFooter.count()) {
        await callFooter.scrollIntoViewIfNeeded();
        layout.footerReachable = await callFooter.evaluate(el => { const r = el.getBoundingClientRect(); return r.top >= -1 && r.bottom <= innerHeight + 1; });
      }
      results.push({ id, scene, width, height, scheme, errors, externalRequests, violations: audit, ...layout });
      console.log(JSON.stringify({ id, violations: audit.length, overflow: layout.overflow, clippedControls: layout.clippedControls.length, nestedScrollChains: layout.nestedScrollChains.length, footerReachable: layout.footerReachable, errors: errors.length }));
    } catch (error) {
      await page.screenshot({ path: resolve(out, `${id}-failed.png`), fullPage: true });
      results.push({ id, error: String(error), errors, externalRequests });
      console.log(JSON.stringify({ id, error: String(error) }));
    } finally {
      await page.close();
      writeFileSync(resolve(out, "metrics.json"), JSON.stringify({ recordedAt: new Date().toISOString(), results }, null, 2));
    }
  } };
  await Promise.all(Array.from({ length: Math.max(1, Number(process.env.UI_WORKERS ?? 2)) }, worker));
} finally { await browser.close(); }
if (results.some(r => r.error || r.errors.length || r.externalRequests.length || r.overflow || r.clippedControls?.length || r.violations?.length || r.nestedScrollChains?.length || r.footerReachable === false)) process.exitCode = 1;
