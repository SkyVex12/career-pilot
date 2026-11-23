// Real desktop React controls; fictional IPC only, with no audio or provider requests.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const require = createRequire(resolve(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");
const out = resolve(process.argv[2] ?? ".implementation/live-language-ui");
const languages = JSON.parse(readFileSync(new URL("./languages.json", import.meta.url), "utf8"));
const axe = readFileSync("apps/desktop/node_modules/axe-core/axe.min.js", "utf8");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge", headless: true });
const results = [];
try {
  for (const scheme of ["light", "dark"]) for (const width of [920, 400]) for (const surface of ["main", "overlay", "compact"]) {
    const id = `${surface}-${width}-${scheme}`;
    const page = await browser.newPage({ viewport: { width, height: width === 400 ? 520 : 720 }, colorScheme: scheme, reducedMotion: "reduce" });
    page.setDefaultTimeout(8000);
    const errors = [], externalRequests = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === "http://127.0.0.1:1430") return route.continue();
      externalRequests.push(route.request().url()); return route.abort();
    });
    try {
      await page.goto(`http://127.0.0.1:1430/?call=midcall${surface === "compact" ? "&compact=1" : ""}${surface === "main" ? "" : "#overlay"}`);
      const language = page.getByRole("button", { name: "Call language", exact: true });
      await language.waitFor();
      await page.waitForFunction(() => !document.querySelector('button[aria-label="Call language"]')?.disabled);
      await language.scrollIntoViewIfNeeded();
      await page.waitForTimeout(100);
      await language.click();
      const search = page.getByRole("combobox", { name: "Search call languages", exact: true });
      await search.waitFor();
      assert.equal(await page.getByRole("option").count(), languages.length);
      await search.fill("tieng viet");
      assert.equal(await page.getByRole("option").count(), 1);
      await page.keyboard.press("Enter");
      await page.waitForFunction(() => { const button = document.querySelector('button[aria-label="Call language"]'); return button?.textContent.includes("Vietnamese") && !button.disabled; });
      assert.equal(await language.isDisabled(), false);
      await language.click();
      await search.fill("العربية");
      assert.equal(await page.getByRole("option").count(), 1);
      const rtl = page.getByRole("option").locator("bdi");
      assert.equal(await rtl.getAttribute("dir"), "rtl");
      await page.addScriptTag({ content: axe });
      const violations = await page.evaluate(async () => (await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] } })).violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) })));
      const layout = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth > innerWidth,
        clippedControls: [...document.querySelectorAll("button,input,textarea")].filter(el => {
          if (!el.getClientRects().length) return false;
          const r = el.getBoundingClientRect(); return r.left < -1 || r.right > innerWidth + 1;
        }).map(el => el.getAttribute("aria-label") || el.textContent.trim()),
      }));
      await page.screenshot({ path: resolve(out, `${id}.png`) });
      await page.keyboard.press("Escape");
      assert.equal(await language.innerText(), "Tiếng Việt · Vietnamese");
      assert(await language.evaluate(el => el === document.activeElement));
      results.push({ id, languages: languages.length, errors, externalRequests, violations, ...layout });
    } catch (error) {
      await page.screenshot({ path: resolve(out, `${id}-failed.png`) });
      results.push({ id, error: String(error), errors, externalRequests });
    } finally { await page.close(); }
  }
} finally { await browser.close(); }
writeFileSync(resolve(out, "metrics.json"), JSON.stringify({ recordedAt: new Date().toISOString(), results }, null, 2));
console.log(JSON.stringify(results));
if (results.some(r => r.error || r.errors.length || r.externalRequests.length || r.violations?.length || r.overflow || r.clippedControls?.length)) process.exitCode = 1;
