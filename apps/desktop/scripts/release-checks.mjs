import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const script = fileURLToPath(new URL("./release.mjs", import.meta.url));
function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), "copilot-release-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const cfg = join(dir, "release.json");
  writeFileSync(cfg, JSON.stringify({ stage: "test", api_url: "https://example.com/api", gateway_url: "wss://example.com", update_url: "https://example.com/desktop/latest.json", updater_pubkey: "test" }));
  const run = (...args) => spawnSync(process.execPath, [script, ...args], { encoding: "utf8", env: { ...process.env, COPILOT_RELEASE_CONFIG: cfg } });
  return { dir, run };
}

test("Mac config selects DMG and app; preview does not require an updater key", (t) => {
  const { dir, run } = fixture(t);
  for (const platform of ["darwin-aarch64", "darwin-x86_64", "windows-x86_64"]) {
    const out = join(dir, `${platform}.json`);
    const result = run("config", "--version", "1.2.3", "--platform", platform, "--out", out);
    assert.equal(result.status, 0, result.stderr);
    const cfg = JSON.parse(readFileSync(out));
    assert.deepEqual(cfg.bundle.targets, platform.startsWith("darwin") ? ["app", "dmg"] : ["nsis"]);
    assert.equal(cfg.bundle.createUpdaterArtifacts, true);
  }
  const out = join(dir, "preview.json");
  assert.equal(run("config", "--version", "0.0.0-ci", "--platform", "darwin-aarch64", "--preview", "--out", out).status, 0);
  assert.equal(JSON.parse(readFileSync(out)).bundle.createUpdaterArtifacts, false);
  assert.notEqual(run("config", "--version", "1.2.3", "--platform", "typo", "--out", out).status, 0);
});

test("packages both Mac architectures and Windows, merging all signed updater entries", (t) => {
  const { dir, run } = fixture(t);
  const manifests = [];
  for (const platform of ["windows-x86_64", "darwin-aarch64", "darwin-x86_64"]) {
    const mac = platform.startsWith("darwin");
    const arch = platform.split("-")[1];
    const root = join(dir, platform);
    const bundle = join(root, mac ? "macos" : "nsis");
    mkdirSync(bundle, { recursive: true });
    const source = join(bundle, mac ? "Copilot Desktop.app.tar.gz" : "Copilot Desktop_1.2.3_x64-setup.exe");
    writeFileSync(source, platform);
    writeFileSync(`${source}.sig`, `signed-${platform}`);
    if (mac) {
      mkdirSync(join(root, "dmg"));
      writeFileSync(join(root, "dmg", `Copilot Desktop_1.2.3_${arch === "x86_64" ? "x64" : "aarch64"}.dmg`), "disk image");
    }
    const out = join(root, "out");
    const result = run("manifest", "--version", "1.2.3", "--platform", platform, "--bundle-dir", bundle, "--out", out);
    assert.equal(result.status, 0, result.stderr);
    const manifest = join(out, "latest.json");
    manifests.push(manifest);
    const entry = JSON.parse(readFileSync(manifest)).platforms[platform];
    assert.equal(entry.signature, `signed-${platform}`);
    assert.equal(readFileSync(join(out, "1.2.3", entry.url.split("/").at(-1)), "utf8"), platform);
    assert.ok(readFileSync(join(out, mac ? `Copilot-${arch}.dmg` : "Copilot-setup.exe")).length);
  }
  const out = join(dir, "latest.json");
  const result = run("merge", "--version", "1.2.3", "--out", out, ...manifests);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(Object.keys(JSON.parse(readFileSync(out)).platforms).length, 3);
  assert.notEqual(run("merge", "--version", "1.2.3", "--out", out, manifests[0], manifests[0]).status, 0);
  assert.notEqual(run("merge", "--version", "1.2.4", "--out", out, ...manifests).status, 0);
});

test("missing signature stops packaging", (t) => {
  const { dir, run } = fixture(t);
  writeFileSync(join(dir, "Copilot Desktop.app.tar.gz"), "unsigned");
  const result = run("manifest", "--version", "1.2.3", "--platform", "darwin-aarch64", "--bundle-dir", dir, "--out", join(dir, "out"));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /TAURI_SIGNING_PRIVATE_KEY/);
});
