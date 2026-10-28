// What the update status bar shows, and how the windows report the user's activity to the
// updater (src-tauri/src/release.rs: an update installs only when no session runs, at once if
// the app is untouched, else after two quiet minutes). The updater sends its state at every
// step (checking, downloading, ready, installing), so the bar's text changes with it. Pure,
// so tests/updates.test.ts covers it.
import type { UpdateState } from "./api";

export type Bar =
  | { kind: "none" }
  | {
      kind: "bar";
      tone: "busy" | "done" | "error";
      text: string;
      /** 0–100; null: under way, size unknown (a moving bar); undefined: no bar. */
      progress?: number | null;
      /** The install button: "Install and restart" (ask me first) or "Restart now" (automatic). */
      action?: { label: string; enabled: boolean };
      dismissible: boolean;
    };

/** "about 2 minutes" for a waiting update's countdown. */
export function soon(seconds: number): string {
  if (seconds >= 90) return `about ${Math.round(seconds / 60)} minutes`;
  if (seconds >= 45) return "about a minute";
  return "a few seconds";
}

/** The status bar under the tabs. Only while an update is under way, waiting, or just done;
 * checks show in the settings. */
export function bar(update: UpdateState, running: boolean): Bar {
  switch (update.state) {
    case "none":
    case "checking":
    case "up_to_date":
    case "check_failed":
      return { kind: "none" };
    case "downloading":
      return {
        kind: "bar",
        tone: "busy",
        text:
          update.percent === null
            ? `Downloading version ${update.version}…`
            : `Downloading version ${update.version}… ${update.percent}%`,
        progress: update.percent,
        dismissible: false,
      };
    case "ready": {
      const v = update.version;
      if (!update.auto) {
        return {
          kind: "bar",
          tone: "busy",
          text: running ? `Version ${v} is ready; install it after this call.` : `Version ${v} is ready.`,
          progress: 100,
          action: { label: "Install and restart", enabled: !running },
          dismissible: false,
        };
      }
      let text: string;
      if (running) text = `Version ${v} is ready. It installs after this call.`;
      else if (update.install_in_s === null || update.install_in_s <= 0) text = `Version ${v} is ready. Installing…`;
      else text = `Version ${v} is ready. It installs and restarts in ${soon(update.install_in_s)} if you leave the app alone.`;
      return {
        kind: "bar",
        tone: "busy",
        text,
        progress: 100,
        action: { label: "Restart now", enabled: !running },
        dismissible: false,
      };
    }
    case "installing":
      return {
        kind: "bar",
        tone: "busy",
        text: `Installing version ${update.version}. The app closes and opens again by itself…`,
        progress: null,
        dismissible: false,
      };
    case "updated":
      return { kind: "bar", tone: "done", text: `Updated to version ${update.version}.`, dismissible: true };
    case "outdated":
      return { kind: "bar", tone: "error", text: update.message, dismissible: false };
    case "failed":
      return { kind: "bar", tone: "error", text: update.message, dismissible: true };
  }
}

/** The settings' update line, live; null: nothing to say beyond the last check. */
export function settingsLine(update: UpdateState, now: number = Date.now()): string | null {
  switch (update.state) {
    case "none":
      return null;
    case "checking":
      return "Checking for updates…";
    case "up_to_date":
      return `Version ${update.version} is the newest (checked ${ago(update.checked_at, now)}).`;
    case "check_failed":
      return update.message;
    default: {
      const b = bar(update, false);
      return b.kind === "bar" ? b.text : null;
    }
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
