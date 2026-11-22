// Shared-token smoke checks for the independent overlay, without capture or live requests.
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const require = createRequire(resolve(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");
const out = resolve(process.argv[2] ?? "docs/design/screens/production-ui/overlay-final");
const axe = readFileSync("apps/desktop/node_modules/axe-core/axe.min.js", "utf8");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge", headless: true });
const results = [];
try {
  for (const scheme of ["light", "dark"]) for (const viewport of [{ width: 760, height: 620 }, { width: 400, height: 520 }]) {
    const id = `overlay-${viewport.width}-${scheme}`;
    const page = await browser.newPage({ viewport, colorScheme: scheme, reducedMotion: "reduce" });
    page.setDefaultTimeout(10000);
    const errors = [], externalRequests = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === "http://127.0.0.1:1430") return route.continue();
      externalRequests.push(route.request().url()); return route.abort();
    });
    try {
      await page.goto("http://127.0.0.1:1430/?call=midcall#overlay");
      await page.getByRole("tablist", { name: "Call tools" }).waitFor();
      await page.waitForFunction(() => {
        const answer = document.querySelector(".ov-ask > .ov-ask-btn");
        return answer && !answer.disabled;
      });
      await page.waitForTimeout(200);
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
      results.push({ id, viewport, scheme, errors, externalRequests, violations, ...layout });
    } catch (error) {
      await page.screenshot({ path: resolve(out, `${id}-failed.png`) });
      results.push({ id, error: String(error), errors, externalRequests });
    } finally { await page.close(); }
  }
} finally { await browser.close(); }
writeFileSync(resolve(out, "metrics.json"), JSON.stringify({ recordedAt: new Date().toISOString(), results }, null, 2));
console.log(JSON.stringify(results));
if (results.some(r => r.error || r.errors.length || r.externalRequests.length || r.violations?.length || r.overflow || r.clippedControls?.length)) process.exitCode = 1;
