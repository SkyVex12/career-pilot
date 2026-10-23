// What the update banner shows, and how the windows report the user's activity to the
// updater (src-tauri/src/release.rs: an update installs only when no session runs, and waits
// for a quiet moment). Pure, so tests/updates.test.ts covers it.
import type { UpdateState } from "./api";

export type Banner =
  | { kind: "none" }
  | { kind: "note"; text: string; dismissible: boolean }
  /** "Ask me first": the downloaded version, with the install button. */
  | { kind: "ask"; version: string; text: string; canInstall: boolean };

export function banner(update: UpdateState, running: boolean): Banner {
  switch (update.state) {
    case "none":
      return { kind: "none" };
    case "ready":
      // Automatic: nothing to do; it installs by itself when no session runs.
      if (update.auto) return { kind: "none" };
      return {
        kind: "ask",
        version: update.version,
        text: running ? `Version ${update.version} is ready; install it after this session.` : `Version ${update.version} is ready.`,
        canInstall: !running,
      };
    case "installing":
      return { kind: "note", text: `Installing version ${update.version}; the app restarts…`, dismissible: false };
    case "updated":
      return { kind: "note", text: `Updated to ${update.version}.`, dismissible: true };
    case "outdated":
      return { kind: "note", text: update.message, dismissible: false };
    case "failed":
      // Retried by itself; the details are in the settings' update log.
      return { kind: "none" };
  }
}

/** Calls `report` at most once per `everyMs` (a click, key or scroll in a window). */
export function throttled(report: () => void, everyMs: number, now: () => number = Date.now): () => void {
  let last = -Infinity;
  return () => {
    const t = now();
    if (t - last < everyMs) return;
    last = t;
    report();
  };
}

/** "3 minutes ago" for the settings' last check. */
export function ago(ms: number, now: number = Date.now()): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(m / 60);
  return `${h} hour${h === 1 ? "" : "s"} ago`;
}
