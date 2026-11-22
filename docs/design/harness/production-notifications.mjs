// Floating notices must not resize the page or hide its primary action. All events are fixtures.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
const require = createRequire(resolve(process.env.PW_DIR ?? process.cwd(), "package.json"));
const { chromium } = require("playwright-core");
const out = resolve(process.argv[2] ?? "docs/design/screens/production-ui/notifications");
const axe = readFileSync("apps/desktop/node_modules/axe-core/axe.min.js", "utf8");
const sizes = (process.env.UI_SIZES ?? "1080x780,920x720,720x520,390x780,400x240").split(",").map(size => { const [width, height] = size.split("x").map(Number); return { width, height }; });
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? "msedge", headless: true });
const results = [];
try {
  for (const scheme of ["light", "dark"]) for (const viewport of sizes) {
    const id = `notices-${viewport.width}-${scheme}`;
    const page = await browser.newPage({ viewport, colorScheme: scheme, reducedMotion: "reduce" });
    page.setDefaultTimeout(8000);
    const errors = [], externalRequests = [], layouts = [], actionReachability = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === "http://127.0.0.1:1430") return route.continue();
      externalRequests.push(route.request().url()); return route.abort();
    });
    const pageBounds = () => page.evaluate(() => {
      const rect = selector => {
        const el = document.querySelector(selector), b = el?.getBoundingClientRect();
        return b ? { top: b.top, left: b.left, width: b.width, height: b.height } : null;
      };
      return { heading: rect(".call-design h2,.call-design h1"), footer: rect(".call-design-footer"), body: rect(".call-design-body"), scrollHeight: document.querySelector(".call-design-body")?.scrollHeight };
    });
    const audit = async name => {
      const layout = await page.evaluate(() => {
        const stack = document.querySelector(".ui-toast-viewport"), r = stack.getBoundingClientRect();
        const footer = document.querySelector(".call-design-footer")?.getBoundingClientRect();
        const action = document.querySelector(".call-design-footer .session-start"), a = action?.getBoundingClientRect();
        const hit = a && document.elementFromPoint(a.left + a.width / 2, a.top + a.height / 2);
        return { position: getComputedStyle(stack).position, left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: innerWidth, height: innerHeight, footerTop: footer?.top,
          footerActionUncovered: Boolean(hit && action.contains(hit)),
          notifications: [...stack.querySelectorAll(".app-notification")].map(el => { const n = el.getBoundingClientRect(); return { name: el.getAttribute("aria-label"), left: n.left, top: n.top, right: n.right, bottom: n.bottom }; }),
        };
      });
      assert.equal(layout.position, "fixed", "Notifications must be independent of page layout");
      assert(layout.left >= -1 && layout.right <= layout.width + 1 && layout.top >= -1 && layout.bottom <= layout.height + 1, `Notification stack clipped: ${JSON.stringify(layout)}`);
      assert(layout.width - layout.right <= 24, "Notifications must sit at the right edge");
      assert(layout.bottom <= layout.footerTop + 1, "Notifications cover the call action footer");
      assert(layout.footerActionUncovered, "A floating element blocks the Start call button");
      assert.deepEqual(await pageBounds(), baseline, "Notice changed page heading, footer, or scroll dimensions");
      const violations = await page.evaluate(async () => (await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] } })).violations.map(v => ({ id: v.id, targets: v.nodes.map(n => n.target) })));
      assert.equal(violations.length, 0, JSON.stringify(violations));
      layouts.push({ name, ...layout, violations });
    };
    const checkActions = async name => {
      const actions = page.locator(".ui-toast-viewport button");
      for (let index = 0; index < await actions.count(); index++) {
        const action = actions.nth(index);
        await action.scrollIntoViewIfNeeded();
        await action.focus();
        const result = await action.evaluate(el => {
          const r = el.getBoundingClientRect(), stack = el.closest(".ui-toast-viewport").getBoundingClientRect();
          const x = r.left + r.width / 2, y = r.top + r.height / 2;
          return { name: el.getAttribute("aria-label") || el.textContent.trim(), focused: document.activeElement === el,
            reachable: x >= stack.left && x <= stack.right && y >= stack.top && y <= stack.bottom && el.contains(document.elementFromPoint(x, y)) };
        });
        assert(result.focused && result.reachable, `Notification action inaccessible: ${JSON.stringify(result)}`);
        assert.deepEqual(await pageBounds(), baseline, "Scrolling a notice moved the page");
        actionReachability.push({ state: name, ...result });
      }
    };
    let baseline;
    try {
      await page.goto("http://127.0.0.1:1430/");
      await page.getByRole("navigation", { name: "Main", exact: true }).waitFor();
      await page.waitForTimeout(250);
      await page.addScriptTag({ content: axe });
      baseline = await pageBounds();
      await page.locator(".call-design-footer .session-start").focus();
      await page.evaluate(() => window.__harness.server({ state: "starting", detail: "Preparing your workspace" }));
      await page.getByRole("region", { name: "Starting the server", exact: true }).waitFor();
      await audit("server-starting");
      await page.evaluate(() => window.__harness.update({ state: "downloading", version: "1.2.3", percent: 27 }));
      await page.getByRole("region", { name: "App update", exact: true }).waitFor();
      await audit("server-and-update");
      assert(await page.locator(".call-design-footer .session-start").evaluate(el => el === document.activeElement), "Notifications stole keyboard focus");
      await page.screenshot({ path: resolve(out, `${id}.png`) });
      await checkActions("server-and-update");
      await page.getByRole("button", { name: "Dismiss starting the server notification", exact: true }).click();
      await page.evaluate(() => window.__harness.server({ state: "starting", detail: "Services are starting" }));
      assert.equal(await page.getByRole("region", { name: "Starting the server", exact: true }).count(), 0, "Detail update resurrected dismissed notice");
      await page.getByRole("button", { name: "Dismiss app update notification", exact: true }).click();
      await page.evaluate(() => window.__harness.update({ state: "downloading", version: "1.2.3", percent: 72 }));
      assert.equal(await page.getByRole("region", { name: "App update", exact: true }).count(), 0, "Progress update resurrected dismissed notice");
      await page.evaluate(() => window.__harness.server({ state: "failed", detail: "The test server is unavailable" }));
      await page.getByRole("region", { name: "Server unavailable", exact: true }).waitFor();
      await audit("server-failed");
      await page.evaluate(() => window.__harness.update({ state: "ready", version: "1.2.3", auto: false, install_in_s: null }));
      await page.getByRole("button", { name: "Install and restart", exact: true }).waitFor();
      await audit("server-failed-and-update-ready");
      await checkActions("server-failed-and-update-ready");
      await page.evaluate(() => window.__harness.server({ state: "ready", detail: null }));
      await page.getByRole("region", { name: "Server unavailable", exact: true }).waitFor({ state: "hidden" });
      await page.getByRole("button", { name: "Install and restart", exact: true }).waitFor();
      await audit("update-ready");
      assert.equal(errors.length, 0, JSON.stringify(errors));
      assert.equal(externalRequests.length, 0, JSON.stringify(externalRequests));
      results.push({ id, status: "passed", baseline, layouts, actionReachability, errors, externalRequests });
    } catch (error) {
      await page.screenshot({ path: resolve(out, `${id}-failed.png`) });
      results.push({ id, status: "failed", error: String(error), baseline, layouts, actionReachability, errors, externalRequests });
    } finally {
      await page.close();
      writeFileSync(resolve(out, "metrics.json"), JSON.stringify({ recordedAt: new Date().toISOString(), results }, null, 2));
    }
    console.log(JSON.stringify(results.at(-1)));
  }
} finally { await browser.close(); }
if (results.some(result => result.status !== "passed")) process.exitCode = 1;
