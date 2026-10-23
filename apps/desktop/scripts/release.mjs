// Release builds of the desktop app (README.md, "Releasing the desktop app"). Two steps around
// `tauri build`, both reading apps/desktop/release.json (or the file COPILOT_RELEASE_CONFIG
// names), the same file build.rs embeds:
//
//   node scripts/release.mjs config --version 0.2.0
//       writes src-tauri/tauri.release.conf.json: the version, the NSIS installer, updater
//       artifacts, the updater's endpoint and public key, and the Windows signing command if
//       WINDOWS_SIGN_COMMAND is set (a drop-in for code signing later)
//   node scripts/release.mjs manifest --version 0.2.0 --out dist-release
//       copies the installer and its signature from target/release/bundle/nsis into the
//       folder, as they are published to the releases bucket, and writes latest.json, the
//       update manifest the installed apps check (notes from --notes or COPILOT_RELEASE_NOTES)
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

function args(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
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
  const local = /^http:\/\/(127\.0\.0\.1|localhost)[:/]/.test(cfg.update_url);
  if (cfg.update_url.startsWith("http://") && !local) fail("update_url must be https (http only for a local test)");
  const overlay = {
    version: v,
    bundle: {
      targets: ["nsis"],
      createUpdaterArtifacts: true,
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
  const out = join(desktop, "src-tauri", "tauri.release.conf.json");
  writeFileSync(out, JSON.stringify(overlay, null, 2) + "\n");
  console.log(`wrote ${out} (version ${v}, updates from ${cfg.update_url})`);
}

function writeManifest(a) {
  const cfg = releaseConfig();
  const v = version(a);
  const bundle = a["bundle-dir"] ? resolve(a["bundle-dir"]) : join(repo, "target", "release", "bundle", "nsis");
  const name = installerName(v);
  const installer = join(bundle, builtName(v));
  if (!existsSync(installer)) fail(`${installer} not found: build first`);
  if (!existsSync(`${installer}.sig`)) fail(`${installer}.sig not found: TAURI_SIGNING_PRIVATE_KEY was not set for the build`);
  const out = resolve(a.out ?? "dist-release");
  mkdirSync(join(out, v), { recursive: true });
  // <base>/<version>/Copilot-Desktop_<version>_x64-setup.exe(.sig), kept; <base>/Copilot-setup.exe,
  // the newest, for the download link; <base>/latest.json, written last.
  copyFileSync(installer, join(out, v, name));
  copyFileSync(`${installer}.sig`, join(out, v, `${name}.sig`));
  copyFileSync(installer, join(out, "Copilot-setup.exe"));
  const manifest = {
    version: v,
    // --notes, or COPILOT_RELEASE_NOTES (release-desktop.yml: the merged pull requests' titles).
    notes: typeof a.notes === "string" ? a.notes : process.env.COPILOT_RELEASE_NOTES?.trim() || `Copilot ${v}`,
    pub_date: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    platforms: {
      "windows-x86_64": {
        signature: readFileSync(`${installer}.sig`, "utf8").trim(),
        url: `${baseUrl(cfg)}/${v}/${name}`,
      },
    },
  };
  writeFileSync(join(out, "latest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log(`wrote ${out}: ${v}/${name}, Copilot-setup.exe, latest.json -> ${manifest.platforms["windows-x86_64"].url}`);
}

const a = args(process.argv.slice(2));
switch (a._[0]) {
  case "config":
    writeConfig(a);
    break;
  case "manifest":
    writeManifest(a);
    break;
  default:
    fail("usage: release.mjs config --version X | manifest --version X [--out DIR] [--bundle-dir DIR] [--notes TEXT]");
}
