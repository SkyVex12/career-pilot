import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { inspectMachO, makeReceipt, validateReceipt, verifyApp } from "./macos-bundle.mjs";

function image(arch, minimum = 13 << 16, { legacy = false, platform = 1, dependency } = {}) {
  const cpu = arch === "aarch64" ? 0x100000c : 0x1000007;
  const deploymentSize = legacy ? 16 : 24;
  const dependencySize = dependency ? Math.ceil((25 + Buffer.byteLength(dependency)) / 8) * 8 : 0;
  const data = Buffer.alloc(32 + deploymentSize + dependencySize);
  [0xfeedfacf, cpu, 0, 2, dependency ? 2 : 1, deploymentSize + dependencySize, 0, 0].forEach((value, index) => data.writeUInt32LE(value, index * 4));
  data.writeUInt32LE(legacy ? 0x24 : 0x32, 32);
  data.writeUInt32LE(deploymentSize, 36);
  if (!legacy) data.writeUInt32LE(platform, 40);
  data.writeUInt32LE(minimum, legacy ? 40 : 44);
  if (dependency) {
    const offset = 32 + deploymentSize;
    data.writeUInt32LE(0xc, offset);
    data.writeUInt32LE(dependencySize, offset + 4);
    data.writeUInt32LE(24, offset + 8);
    data.write(dependency, offset + 24);
  }
  return data;
}

function universal(slices = [image("aarch64"), image("x86_64")], wide = false) {
  const recordSize = wide ? 32 : 20;
  let offset = 8 + slices.length * recordSize;
  const table = Buffer.alloc(offset);
  table.writeUInt32BE(wide ? 0xcafebabf : 0xcafebabe, 0);
  table.writeUInt32BE(slices.length, 4);
  slices.forEach((slice, index) => {
    const record = 8 + index * recordSize;
    table.writeUInt32BE(slice.readUInt32LE(4), record);
    if (wide) {
      table.writeBigUInt64BE(BigInt(offset), record + 8);
      table.writeBigUInt64BE(BigInt(slice.length), record + 16);
    } else {
      table.writeUInt32BE(offset, record + 8);
      table.writeUInt32BE(slice.length, record + 12);
    }
    offset += slice.length;
  });
  return Buffer.concat([table, ...slices]);
}

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), "copilot-universal-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const app = join(dir, "Copilot Desktop.app");
  mkdirSync(join(app, "Contents/MacOS"), { recursive: true });
  writeFileSync(join(app, "Contents/MacOS/copilot-desktop"), universal());
  const info = { LSMinimumSystemVersion: "13.0", CFBundleExecutable: "copilot-desktop" };
  const dmg = join(dir, "Copilot Desktop_1.2.3_universal.dmg");
  writeFileSync(dmg, "verified Universal disk image");
  return { dir, app, info, dmg };
}

test("reads both Universal table formats and legacy deployment commands", () => {
  for (const wide of [false, true]) {
    const result = inspectMachO(universal([image("aarch64"), image("x86_64", 12 << 16, { legacy: true })], wide));
    assert.deepEqual(result.map(slice => [slice.arch, slice.minimum]), [["aarch64", "13.0.0"], ["x86_64", "12.0.0"]]);
  }
});

test("rejects malformed, overlapping and non-macOS Mach-O slices", () => {
  const overlap = universal();
  overlap.writeUInt32BE(48, 8 + 20 + 8);
  assert.throws(() => inspectMachO(overlap), /Overlapping/);
  const truncated = universal().subarray(0, 50);
  assert.throws(() => inspectMachO(truncated), /slice bounds/);
  assert.throws(() => inspectMachO(image("aarch64", 13 << 16, { platform: 2 })), /does not target macOS/);
  assert.throws(() => inspectMachO(universal([image("aarch64"), image("aarch64")])), /slice architecture/);
  assert.throws(() => inspectMachO(image("x86_64", 13 << 16, { dependency: "/opt/homebrew/lib/libtest.dylib" })), /outside the app/);
});

test("verifies all bundled native images and rejects a newer or single-architecture dependency", t => {
  const { app, info } = fixture(t);
  mkdirSync(join(app, "Contents/Frameworks"));
  const library = join(app, "Contents/Frameworks/runtime.dylib");
  writeFileSync(library, universal([image("aarch64", 12 << 16), image("x86_64", 12 << 16)]));
  assert.equal(verifyApp(app, info).images.length, 2);
  writeFileSync(library, universal([image("aarch64"), image("x86_64", (13 << 16) + 256)]));
  assert.throws(() => verifyApp(app, info), /requires macOS 13.1.0/);
  writeFileSync(library, image("aarch64"));
  assert.throws(() => verifyApp(app, info), /not Universal/);
});

test("requires macOS 13 metadata and the declared Universal executable", t => {
  const { app, info } = fixture(t);
  assert.throws(() => verifyApp(app, { ...info, LSMinimumSystemVersion: "15.4" }), /must require macOS 13.0/);
  assert.throws(() => verifyApp(app, { ...info, CFBundleExecutable: "missing" }), /executable is not/);
  assert.throws(() => verifyApp(app, { ...info, CFBundleExecutable: "../other" }), /Invalid app executable/);
});

test("preview reuse requires the verified Universal metadata, exact source SHA and unchanged DMG", t => {
  const { dir, app, info, dmg } = fixture(t);
  const sha = "a".repeat(40);
  const receipt = makeReceipt(dmg, sha, verifyApp(app, info));
  assert.equal(validateReceipt(receipt, dir, sha), dmg);
  assert.throws(() => validateReceipt(receipt, dir, "b".repeat(40)), /source commit/);
  assert.throws(() => validateReceipt({ ...receipt, app: { ...receipt.app, architectures: ["aarch64"] } }, dir, sha), /not a verified/);
  assert.throws(() => validateReceipt({ ...receipt, dmg: "../other.dmg" }, dir, sha), /filename/);
  writeFileSync(dmg, "different disk image");
  assert.throws(() => validateReceipt(receipt, dir, sha), /does not match/);
});

test("preview staging produces one Universal download and identical compatibility aliases", t => {
  const { dir, app, info, dmg } = fixture(t);
  const sha = "a".repeat(40);
  writeFileSync(join(dir, "verification.json"), JSON.stringify(makeReceipt(dmg, sha, verifyApp(app, info))));
  const script = fileURLToPath(new URL("./macos-bundle.mjs", import.meta.url));
  const out = join(dir, "publish");
  const run = () => spawnSync(process.execPath, [script, "preview", "--artifacts", dir, "--sha", sha, "--out", out], { encoding: "utf8" });
  const result = run();
  assert.equal(result.status, 0, result.stderr);
  for (const alias of ["universal", "aarch64", "x86_64"]) {
    assert.deepEqual(readFileSync(join(out, `Copilot-${alias}.dmg`)), readFileSync(dmg));
  }
  writeFileSync(join(dir, "older-aarch64.dmg"), "old single-architecture app");
  const mixed = run();
  assert.notEqual(mixed.status, 0);
  assert.match(mixed.stderr, /exactly the verified Universal DMG/);
});
