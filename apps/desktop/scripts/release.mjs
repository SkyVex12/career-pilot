// Platform-specific desktop release bundles and the shared updater manifest.
// config --version X [--platform windows-x86_64|darwin-aarch64|darwin-x86_64|darwin-universal]
//   [--preview] [--out FILE]: write the Tauri release overlay (default: Windows).
// manifest --version X [--platform ...] [--bundle-dir DIR] [--out DIR]: stage the
//   installer, signed update archive, and one platform's latest.json.
// merge --version X --out FILE MANIFEST...: combine platforms after all builds succeed.
// See docs/macos.md for Apple signing and Universal Mac distribution.
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const desktop = resolve(here, "..");
const repo = resolve(desktop, "../..");

const SEMVER = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;
// The installer as Tauri's NSIS bundler names it (<productName>_<version>_x64-setup.exe), and as
// it is published (no space, for plain URLs). productName is "Copilot Desktop", so it installs
// to %LOCALAPPDATA%\Copilot Desktop: other tools use %LOCALAPPDATA%\Copilot.
const builtName = (version) => `Copilot Desktop_${version}_x64-setup.exe`;
const installerName = (version) => `Copilot-Desktop_${version}_x64-setup.exe`;

function platform(a) {
  const value = a.platform ?? "windows-x86_64";
  if (!["windows-x86_64", "darwin-aarch64", "darwin-x86_64", "darwin-universal"].includes(value)) fail(`unsupported --platform ${value}`);
  return value;
}

function args(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--preview") { out.preview = true; continue; }
    if (argv[i].startsWith("--")) out[argv[i].slice(2)] = argv[i + 1]?.startsWith("--") ? true : argv[++i] ?? true;
    else out._.push(argv[i]);
  }
  return out;
}

function fail(message) {
  console.error(`release.mjs: ${message}`);
  process.exit(1);
}

function releaseConfig() {
  const path = process.env.COPILOT_RELEASE_CONFIG ? resolve(process.env.COPILOT_RELEASE_CONFIG) : join(desktop, "release.json");
  const cfg = JSON.parse(readFileSync(path, "utf8"));
  for (const key of ["stage", "api_url", "gateway_url", "update_url", "updater_pubkey"]) {
    if (typeof cfg[key] !== "string" || !cfg[key].trim()) fail(`${path}: "${key}" is missing`);
  }
  return cfg;
}

function version(a) {
  // From a tag (desktop-v0.2.0) or a plain version.
  const v = String(a.version ?? "").replace(/^desktop-v/, "").replace(/^v/, "");
  if (!SEMVER.test(v)) fail(`--version ${a.version}: expected 1.2.3 or desktop-v1.2.3`);
  return v;
}

/** Where installers and manifests live: the folder of update_url (…/desktop). */
const baseUrl = (cfg) => cfg.update_url.slice(0, cfg.update_url.lastIndexOf("/"));

function writeConfig(a) {
  const cfg = releaseConfig();
  const v = version(a);
  const mac = platform(a).startsWith("darwin-");
  const local = /^http:\/\/(127\.0\.0\.1|localhost)[:/]/.test(cfg.update_url);
  if (cfg.update_url.startsWith("http://") && !local) fail("update_url must be https (http only for a local test)");
  const overlay = {
    version: v,
    bundle: {
      targets: mac ? ["app", "dmg"] : ["nsis"],
      createUpdaterArtifacts: !a.preview,
      windows: {
        nsis: { installMode: "currentUser" },
        ...(process.env.WINDOWS_SIGN_COMMAND ? { signCommand: process.env.WINDOWS_SIGN_COMMAND } : {}),
      },
    },
    plugins: {
      updater: {
        pubkey: cfg.updater_pubkey,
        endpoints: [cfg.update_url],
        // The signature's trusted comment carries the version it was built for, so a doctored
        // latest.json cannot pair a newer version number with an older signed installer.
        requireSignedVersion: true,
        windows: { installMode: "passive" },
        ...(local ? { dangerousInsecureTransportProtocol: true } : {}),
      },
    },
  };
  const out = a.out ? resolve(a.out) : join(desktop, "src-tauri", "tauri.release.conf.json");
  writeFileSync(out, JSON.stringify(overlay, null, 2) + "\n");
  console.log(`wrote ${out} (version ${v}, updates from ${cfg.update_url})`);
}

function writeManifest(a) {
  const cfg = releaseConfig();
  const v = version(a);
  const target = platform(a);
  const mac = target.startsWith("darwin-");
  const arch = target.slice("darwin-".length);
  const bundle = a["bundle-dir"] ? resolve(a["bundle-dir"]) : join(repo, "target", "release", "bundle", mac ? "macos" : "nsis");
  const name = mac ? `Copilot-Desktop_${v}_${arch}.app.tar.gz` : installerName(v);
  const installer = join(bundle, mac ? "Copilot Desktop.app.tar.gz" : builtName(v));
  if (!existsSync(installer)) fail(`${installer} not found: build first`);
  if (!existsSync(`${installer}.sig`)) fail(`${installer}.sig not found: TAURI_SIGNING_PRIVATE_KEY was not set for the build`);
  const out = resolve(a.out ?? "dist-release");
  mkdirSync(join(out, v), { recursive: true });
  // <base>/<version>/Copilot-Desktop_<version>_x64-setup.exe(.sig), kept; <base>/Copilot-setup.exe,
  // the newest, for the download link; <base>/latest.json, written last.
  copyFileSync(installer, join(out, v, name));
  copyFileSync(`${installer}.sig`, join(out, v, `${name}.sig`));
  if (mac) {
    const dmgArch = arch === "x86_64" ? "x64" : arch;
    const dmg = join(dirname(bundle), "dmg", `Copilot Desktop_${v}_${dmgArch}.dmg`);
    if (!existsSync(dmg)) fail(`${dmg} not found: build the dmg bundle first`);
    copyFileSync(dmg, join(out, v, `Copilot-Desktop_${v}_${arch}.dmg`));
    for (const alias of arch === "universal" ? ["universal", "aarch64", "x86_64"] : [arch]) {
      copyFileSync(dmg, join(out, `Copilot-${alias}.dmg`));
    }
  } else {
    copyFileSync(installer, join(out, "Copilot-setup.exe"));
  }
  // Tauri updaters look up the running CPU, even when the archive is Universal.
  const targets = arch === "universal" ? ["darwin-aarch64", "darwin-x86_64"] : [target];
  const entry = { signature: readFileSync(`${installer}.sig`, "utf8").trim(), url: `${baseUrl(cfg)}/${v}/${name}` };
  const manifest = {
    version: v,
    // --notes, or COPILOT_RELEASE_NOTES (release-desktop.yml: the merged pull requests' titles).
    notes: typeof a.notes === "string" ? a.notes : process.env.COPILOT_RELEASE_NOTES?.trim() || `Copilot ${v}`,
    pub_date: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    platforms: Object.fromEntries(targets.map(key => [key, entry])),
  };
  writeFileSync(join(out, "latest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log(`wrote ${out}: ${v}/${name}, latest.json -> ${entry.url}`);
}

function mergeManifests(a) {
  const v = version(a);
  const files = a._.slice(1);
  if (!files.length) fail("merge needs manifest file paths");
  let merged;
  for (const file of files) {
    const next = JSON.parse(readFileSync(resolve(file), "utf8"));
    if (next.version !== v) fail(`${file}: version does not match ${v}`);
    merged ??= { ...next, platforms: {} };
    for (const [key, entry] of Object.entries(next.platforms)) {
      if (merged.platforms[key]) fail(`duplicate platform ${key}`);
      if (!entry.signature || !entry.url) fail(`${file}: incomplete ${key} artifact`);
      merged.platforms[key] = entry;
    }
  }
  writeFileSync(resolve(a.out ?? "latest.json"), JSON.stringify(merged, null, 2) + "\n");
}

const a = args(process.argv.slice(2));
switch (a._[0]) {
  case "merge":
    mergeManifests(a);
    break;
  case "config":
    writeConfig(a);
    break;
  case "manifest":
    writeManifest(a);
    break;
  default:
    fail("usage: release.mjs config --version X | manifest --version X [--out DIR] [--bundle-dir DIR] [--notes TEXT] [--platform windows-x86_64|darwin-aarch64|darwin-x86_64|darwin-universal] | merge --version X --out FILE MANIFEST...");
}
