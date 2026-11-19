// Inspect the shipped Mach-O images, then bind the verified app to its preview DMG.
import { createHash } from "node:crypto";
import { copyFileSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const ARCHES = ["aarch64", "x86_64"];
const MINIMUM = "13.0";
const SHA = /^[0-9a-f]{40}$/;
const cpuName = cpu => ({ 0x1000007: "x86_64", 0x100000c: "aarch64" })[cpu];
const version = packed => [packed >>> 16, (packed >>> 8) & 255, packed & 255];
const versionText = packed => version(packed).join(".");
const versionNumber = text => {
  if (typeof text !== "string" || !/^\d+\.\d+(?:\.\d+)?$/.test(text)) throw new Error("Invalid macOS minimum version");
  const [major, minor, patch = 0] = text.split(".").map(Number);
  if (major > 65535 || minor > 255 || patch > 255) throw new Error("Invalid macOS minimum version");
  return major * 65536 + minor * 256 + patch;
};
const digest = file => createHash("sha256").update(readFileSync(file)).digest("hex");
const fail = message => { throw new Error(message); };

function thin(data) {
  const magic = data.subarray(0, 4).toString("hex");
  const little = magic === "cffaedfe";
  if (!little && magic !== "feedfacf") fail("Expected a 64-bit Mach-O image");
  if (data.length < 32) fail("Truncated Mach-O header");
  const uint = offset => little ? data.readUInt32LE(offset) : data.readUInt32BE(offset);
  const arch = cpuName(uint(4));
  if (!arch) fail("Unsupported Mach-O architecture");
  const count = uint(16), size = uint(20);
  if (count > size / 8 || 32 + size > data.length) fail("Invalid Mach-O load commands");
  let offset = 32;
  let minimum;
  const dependencies = [];
  for (let index = 0; index < count; index++) {
    const command = uint(offset), length = uint(offset + 4);
    if (length < 8 || offset + length > 32 + size) fail("Invalid Mach-O load command size");
    if (command === 0x32 || command === 0x24) {
      if (length < (command === 0x32 ? 24 : 16)) fail("Truncated Mach-O deployment command");
      if (command === 0x32 && uint(offset + 8) !== 1) fail("Mach-O image does not target macOS");
      const next = uint(offset + (command === 0x32 ? 12 : 8));
      if (!next || (minimum !== undefined && minimum !== next)) fail("Invalid Mach-O deployment version");
      minimum = next;
    }
    if ([0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023].includes(command)) {
      if (length < 24) fail("Truncated Mach-O dependency command");
      const nameOffset = uint(offset + 8);
      if (nameOffset < 24 || nameOffset >= length) fail("Invalid Mach-O dependency name");
      const name = data.subarray(offset + nameOffset, offset + length).toString("utf8").split("\0")[0];
      if (isAbsolute(name) && !name.startsWith("/System/Library/") && !name.startsWith("/usr/lib/")) {
        fail(`Dependency outside the app or macOS: ${name}`);
      }
      dependencies.push(name);
    }
    offset += length;
  }
  if (offset !== 32 + size || minimum === undefined) fail("Mach-O deployment version is missing");
  return { arch, minimum: versionText(minimum), dependencies };
}

export function inspectMachO(data) {
  const magic = data.subarray(0, 4).toString("hex");
  const formats = { cafebabe: [false, false], bebafeca: [true, false], cafebabf: [false, true], bfbafeca: [true, true] };
  const format = formats[magic];
  if (!format) {
    if (["cffaedfe", "feedfacf"].includes(magic)) return [thin(data)];
    if (["cefaedfe", "feedface"].includes(magic)) fail("32-bit Mach-O images are unsupported");
    return null;
  }
  if (data.length < 8) fail("Truncated Universal Mach-O header");
  const [little, wide] = format;
  const uint = offset => little ? data.readUInt32LE(offset) : data.readUInt32BE(offset);
  const long = offset => Number(little ? data.readBigUInt64LE(offset) : data.readBigUInt64BE(offset));
  const count = uint(4), recordSize = wide ? 32 : 20;
  if (!count || count > 16 || 8 + count * recordSize > data.length) fail("Invalid Universal Mach-O architecture table");
  const ranges = [];
  const slices = [];
  for (let index = 0; index < count; index++) {
    const record = 8 + index * recordSize;
    const offset = wide ? long(record + 8) : uint(record + 8);
    const size = wide ? long(record + 16) : uint(record + 12);
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(size) || offset < 8 + count * recordSize || size < 32 || offset + size > data.length) fail("Invalid Universal Mach-O slice bounds");
    if (ranges.some(([start, end]) => offset < end && offset + size > start)) fail("Overlapping Universal Mach-O slices");
    const slice = thin(data.subarray(offset, offset + size));
    if (cpuName(uint(record)) !== slice.arch || slices.some(previous => previous.arch === slice.arch)) fail("Invalid Universal Mach-O slice architecture");
    ranges.push([offset, offset + size]);
    slices.push(slice);
  }
  return slices;
}

export function verifyApp(app, info) {
  if (versionNumber(info.LSMinimumSystemVersion) !== versionNumber(MINIMUM)) fail(`Info.plist must require macOS ${MINIMUM}`);
  const executable = info.CFBundleExecutable;
  if (typeof executable !== "string" || !executable || basename(executable) !== executable || /[\\/]/.test(executable)) fail("Invalid app executable name");
  const root = realpathSync(app);
  const images = [];
  const visit = directory => {
    for (const entry of readdirSync(directory)) {
      const file = join(directory, entry), stat = lstatSync(file);
      if (stat.isSymbolicLink()) {
        const target = relative(root, realpathSync(file));
        if (target === ".." || target.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) || isAbsolute(target)) fail(`App symlink escapes its bundle: ${file}`);
        continue; // Framework symlinks resolve to files inspected in their real directory.
      }
      if (stat.isDirectory()) visit(file);
      else if (stat.isFile()) {
        const slices = inspectMachO(readFileSync(file));
        if (!slices) continue;
        if (JSON.stringify(slices.map(slice => slice.arch).sort()) !== JSON.stringify(ARCHES)) fail(`App image is not Universal: ${file}`);
        for (const slice of slices) {
          if (versionNumber(slice.minimum) > versionNumber(MINIMUM)) fail(`${file} (${slice.arch}) requires macOS ${slice.minimum}, later than ${MINIMUM}`);
        }
        images.push({ path: relative(root, file).replaceAll("\\", "/"), slices });
      }
    }
  };
  visit(root);
  if (!images.some(image => image.path === `Contents/MacOS/${executable}`)) fail("The app executable is not a Universal Mach-O image");
  return { minimumSystemVersion: MINIMUM, architectures: ARCHES, executable, images };
}

export function makeReceipt(dmg, sourceSha, app) {
  if (!SHA.test(sourceSha)) fail("Expected the full source commit SHA");
  return { schema: 1, sourceSha, dmg: basename(dmg), sha256: digest(dmg), app };
}

export function validateReceipt(receipt, directory, sourceSha) {
  if (receipt.schema !== 1 || !SHA.test(sourceSha) || receipt.sourceSha !== sourceSha) fail("Preview verification does not match its source commit");
  const app = receipt.app;
  if (app?.minimumSystemVersion !== MINIMUM || JSON.stringify(app.architectures) !== JSON.stringify(ARCHES) || !Array.isArray(app.images) || !app.images.length) fail("Preview is not a verified macOS 13 Universal app");
  if (!app.images.some(image => image.path === `Contents/MacOS/${app.executable}`)) fail("Preview verification omits the app executable");
  for (const image of app.images) {
    if (!Array.isArray(image.slices) || JSON.stringify(image.slices.map(slice => slice.arch).sort()) !== JSON.stringify(ARCHES) || image.slices.some(slice => versionNumber(slice.minimum) > versionNumber(MINIMUM))) fail("Preview has an incompatible Mach-O image");
  }
  if (typeof receipt.dmg !== "string" || basename(receipt.dmg) !== receipt.dmg || /[\\/]/.test(receipt.dmg) || !receipt.dmg.endsWith(".dmg")) fail("Invalid preview DMG filename");
  const dmg = join(directory, receipt.dmg);
  if (!/^[a-f0-9]{64}$/.test(receipt.sha256) || digest(dmg) !== receipt.sha256) fail("Preview DMG does not match the verified build");
  return dmg;
}

function main(args) {
  const [command, ...rest] = args;
  const options = {};
  for (let index = 0; index < rest.length; index += 2) {
    if (!rest[index].startsWith("--") || !rest[index + 1]) fail("Expected --name value arguments");
    options[rest[index].slice(2)] = rest[index + 1];
  }
  if (command === "verify") {
    const plist = spawnSync("/usr/bin/plutil", ["-convert", "json", "-o", "-", join(options.app, "Contents/Info.plist")], { encoding: "utf8" });
    if (plist.status !== 0) fail(plist.stderr || "Could not read the app Info.plist");
    const app = verifyApp(options.app, JSON.parse(plist.stdout));
    const receipt = makeReceipt(options.dmg, options.sha, app);
    mkdirSync(dirname(resolve(options.out)), { recursive: true });
    writeFileSync(options.out, JSON.stringify(receipt, null, 2) + "\n");
    console.log(`Verified ${app.images.length} Universal Mach-O image(s), macOS ${MINIMUM} or newer.`);
  } else if (command === "preview") {
    const receipt = JSON.parse(readFileSync(join(options.artifacts, "verification.json"), "utf8"));
    const dmg = validateReceipt(receipt, options.artifacts, options.sha);
    const dmgs = readdirSync(options.artifacts).filter(name => name.endsWith(".dmg"));
    if (dmgs.length !== 1 || dmgs[0] !== receipt.dmg) fail("Expected exactly the verified Universal DMG");
    mkdirSync(options.out, { recursive: true });
    for (const name of ["universal", ...ARCHES]) copyFileSync(dmg, join(options.out, `Copilot-${name}.dmg`));
  } else fail("usage: macos-bundle.mjs verify --app APP --dmg DMG --sha SHA --out RECEIPT | preview --artifacts DIR --sha SHA --out DIR");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error(`macos-bundle: ${error.message}`); process.exitCode = 1; }
}
