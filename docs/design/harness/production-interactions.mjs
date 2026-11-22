// Keyboard and enlarged-layout checks with fictional IPC; never starts real capture.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const require = createRequire(resolve(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");
const out = resolve(process.argv[2] ?? "docs/design/screens/production-ui/interactions");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge", headless: true });
const results = [];
try {
  for (const scheme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width: 720, height: 520 }, colorScheme: scheme, reducedMotion: "reduce" });
    page.setDefaultTimeout(8000);
    const errors = [], externalRequests = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === "http://127.0.0.1:1430") return route.continue();
      externalRequests.push(route.request().url()); return route.abort();
    });
    try {
      await page.goto("http://127.0.0.1:1430/");
      const nav = page.getByRole("navigation", { name: "Main", exact: true });
      await nav.waitFor();
      await page.getByRole("button", { name: "Quick settings", exact: true }).filter({ visible: true }).first().focus();
      await page.keyboard.press("Enter");
      await page.getByRole("dialog", { name: "Quick settings", exact: true }).waitFor();
      await page.keyboard.press("Escape");
      await page.getByRole("dialog", { name: "Quick settings", exact: true }).waitFor({ state: "hidden" });
      assert.equal(await page.getByRole("dialog", { name: "Quick settings", exact: true }).count(), 0);
      assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Quick settings");

      const languageSummary = page.locator(".call-options summary");
      await languageSummary.focus();
      await page.keyboard.press("Enter");
      const language = page.getByRole("combobox", { name: "Language", exact: true });
      await page.waitForFunction(() => !document.querySelector('[role="combobox"][aria-label="Language"]')?.disabled);
      await language.scrollIntoViewIfNeeded();
      await page.waitForTimeout(100);
      await language.focus();
      await page.keyboard.press("Enter");
      await page.getByRole("listbox", { name: "Language", exact: true }).waitFor();
      assert.equal(await page.getByRole("option").count(), 24);
      await page.keyboard.press("End");
      await page.keyboard.press("Enter");
      await page.getByRole("listbox", { name: "Language", exact: true }).waitFor({ state: "hidden" });
      assert.equal(await language.getAttribute("aria-expanded"), "false");
      const selectedLanguage = await language.innerText();
      assert.match(selectedLanguage, /Punjabi/);
      await page.screenshot({ path: resolve(out, `keyboard-language-720-${scheme}.png`) });

      await nav.getByRole("button", { name: "Interview prep", exact: true }).focus();
      await page.keyboard.press("Enter");
      const add = page.getByRole("button", { name: "+ Add application", exact: true });
      await add.focus();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Add application", exact: true });
      await dialog.waitFor();
      assert(await dialog.evaluate(el => el.contains(document.activeElement)));
      for (let i = 0; i < 18; i++) {
        await page.keyboard.press(i < 9 ? "Tab" : "Shift+Tab");
        assert(await dialog.evaluate(el => el.contains(document.activeElement)), "Modal focus escaped");
      }
      await page.screenshot({ path: resolve(out, `keyboard-dialog-720-${scheme}.png`) });
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "hidden" });
      assert.equal(await dialog.count(), 0);
      assert(await add.evaluate(el => el === document.activeElement));

      await nav.getByRole("button", { name: "Call", exact: true }).click();
      await page.setViewportSize({ width: 1080, height: 780 });
      // CSS 200% zoom is a layout-stress simulation, not native WebView zoom certification.
      await page.addStyleTag({ content: "html { zoom: 2; }" });
      await page.waitForTimeout(150);
      const enlarged = await page.evaluate(() => ({
        documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
        controls: [...document.querySelectorAll("button,input,textarea,summary")].filter(el => el.getClientRects().length).map(el => {
          const rect = el.getBoundingClientRect();
          return { name: el.getAttribute("aria-label") || el.textContent?.trim().slice(0,80), left: rect.left, right: rect.right };
        }).filter(el => el.left < -1 || el.right > innerWidth + 1),
      }));
      await page.screenshot({ path: resolve(out, `enlarged-layout-200-${scheme}.png`), fullPage: true });
      assert.equal(enlarged.controls.length, 0, `Enlarged controls clipped: ${JSON.stringify(enlarged.controls)}`);
      await page.addStyleTag({ content: "html { zoom: 1; }" });
      await page.setViewportSize({ width: 400, height: 240 });
      await nav.getByRole("button", { name: "Past calls", exact: true }).click();
      const scrollRegion = page.getByRole("region", { name: "Page content", exact: true });
      await page.waitForFunction(() => {
        const el = document.querySelector('[aria-label="Page content"]');
        return el && el.scrollHeight > el.clientHeight + 100;
      });
      await scrollRegion.focus();
      const scrollBefore = await scrollRegion.evaluate(el => el.scrollTop);
      await page.keyboard.press("PageDown");
      await page.waitForTimeout(180);
      const scrollAfter = await scrollRegion.evaluate(el => el.scrollTop);
      assert(scrollAfter > scrollBefore, "Keyboard could not scroll Past calls at the native minimum size");
      await page.screenshot({ path: resolve(out, `keyboard-scroll-400-${scheme}.png`) });
      assert.equal(errors.length, 0, JSON.stringify(errors));
      assert.equal(externalRequests.length, 0, JSON.stringify(externalRequests));
      results.push({ scheme, status: "passed", selectedLanguage, keyboard: ["quick-settings Escape restores focus", "24-language select End/Enter", "application modal trap and Escape focus restoration", "PageDown scrolls Past calls at 400x240"], minimumScroll: { scrollBefore, scrollAfter }, enlarged, errors, externalRequests });
    } catch (error) {
      await page.screenshot({ path: resolve(out, `failure-${scheme}.png`), fullPage: true });
      results.push({ scheme, status: "failed", error: String(error), errors, externalRequests });
    } finally { await page.close(); }
  }
} finally { await browser.close(); }
writeFileSync(resolve(out, "metrics.json"), JSON.stringify({ recordedAt: new Date().toISOString(), results }, null, 2));
console.log(JSON.stringify(results));
if (results.some(result => result.status !== "passed")) process.exitCode = 1;
