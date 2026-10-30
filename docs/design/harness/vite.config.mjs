// The design harness: the desktop UI in a plain browser, with the Rust core's IPC faked by
// fixtures (mock.ts), so every screen and state can be screenshotted without Tauri, the API or
// any model or STT spend. Run from the repo root:
//
//   apps/desktop/node_modules/.bin/vite --config docs/design/harness/vite.config.mjs
//
// It lives under docs/ so CI treats it as documentation; it borrows the desktop app's
// node_modules (resolved from apps/desktop at run time).
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const root = fileURLToPath(new URL("../../../", import.meta.url));
const desktop = `${root}apps/desktop/`;
const fromDesktop = createRequire(`${desktop}package.json`);
const { default: react } = await import(pathToFileURL(fromDesktop.resolve("@vitejs/plugin-react")).href);

export default {
  root: here,
  plugins: [react()],
  resolve: {
    alias: ["@tauri-apps/api", "react-dom", "react"].map((pkg) => ({
      find: new RegExp(`^${pkg}(?=/|$)`),
      replacement: `${desktop}node_modules/${pkg}`,
    })),
  },
  server: {
    port: 1430,
    strictPort: true,
    host: "127.0.0.1",
    fs: { allow: [root] },
  },
};
