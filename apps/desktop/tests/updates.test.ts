import { describe, expect, it } from "vitest";
import { ago, bar, settingsLine, soon, throttled } from "../src/updates";
import type { UpdateState } from "../src/api";

const ready = (auto: boolean, install_in_s: number | null): UpdateState => ({
  state: "ready",
  version: "0.1.2",
  notes: null,
  auto,
  install_in_s,
});

describe("the update status bar", () => {
  it("stays hidden for checks; the settings show those", () => {
    expect(bar({ state: "none" }, false)).toEqual({ kind: "none" });
    expect(bar({ state: "checking" }, false)).toEqual({ kind: "none" });
    expect(bar({ state: "up_to_date", version: "0.1.1", checked_at: 0 }, false)).toEqual({ kind: "none" });
    expect(bar({ state: "check_failed", message: "offline", checked_at: 0 }, false)).toEqual({ kind: "none" });
  });

  it("shows the download's progress, moving until the size is known", () => {
    expect(bar({ state: "downloading", version: "0.1.2", percent: null }, false)).toMatchObject({
      text: "Downloading version 0.1.2…",
      progress: null,
    });
    expect(bar({ state: "downloading", version: "0.1.2", percent: 42 }, false)).toMatchObject({
      text: "Downloading version 0.1.2… 42%",
      progress: 42,
    });
  });

  it("counts down to an automatic install, and offers it now", () => {
    const waiting = bar(ready(true, 110), false);
    expect(waiting).toMatchObject({ action: { label: "Restart now", enabled: true } });
    expect(waiting.kind === "bar" && waiting.text).toContain("in about 2 minutes");
    expect(bar(ready(true, 0), false)).toMatchObject({ text: "Version 0.1.2 is ready. Installing…" });
    const busy = bar(ready(true, null), true);
    expect(busy).toMatchObject({ text: "Version 0.1.2 is ready. It installs after this call.", action: { enabled: false } });
  });

  it("offers the button in ask mode, never during a session", () => {
    expect(bar(ready(false, null), false)).toMatchObject({ action: { label: "Install and restart", enabled: true } });
    const busy = bar(ready(false, null), true);
    expect(busy).toMatchObject({ action: { enabled: false } });
    expect(busy.kind === "bar" && busy.text).toContain("after this call");
  });

  it("says it is installing, then that the app was updated", () => {
    expect(bar({ state: "installing", version: "0.1.2" }, false)).toMatchObject({ tone: "busy", progress: null });
    expect(bar({ state: "updated", version: "0.1.2" }, false)).toEqual({
      kind: "bar",
      tone: "done",
      text: "Updated to version 0.1.2.",
      dismissible: true,
    });
  });

  it("shows failures, which retry by themselves", () => {
    expect(bar({ state: "failed", message: "The download failed." }, false)).toMatchObject({
      tone: "error",
      dismissible: true,
    });
    expect(bar({ state: "outdated", message: "too old" }, false)).toMatchObject({ text: "too old", dismissible: false });
  });
});

describe("the settings' update line", () => {
  it("follows the check", () => {
    expect(settingsLine({ state: "none" })).toBeNull();
    expect(settingsLine({ state: "checking" })).toBe("Checking for updates…");
    expect(settingsLine({ state: "up_to_date", version: "0.1.1", checked_at: 0 }, 30_000)).toBe(
      "Version 0.1.1 is the newest (checked just now).",
    );
    expect(settingsLine({ state: "downloading", version: "0.1.2", percent: 7 })).toBe("Downloading version 0.1.2… 7%");
  });

  it("rounds the countdown", () => {
    expect(soon(110)).toBe("about 2 minutes");
    expect(soon(60)).toBe("about a minute");
    expect(soon(20)).toBe("a few seconds");
  });
});

describe("activity reports", () => {
  it("are throttled", () => {
    let t = 0;
    let calls = 0;
    const report = throttled(() => calls++, 20_000, () => t);
    report();
    t = 5_000;
    report();
    expect(calls).toBe(1);
    t = 20_000;
    report();
    expect(calls).toBe(2);
  });
});

describe("ago", () => {
  it("reads like a person would say it", () => {
    expect(ago(0, 30_000)).toBe("just now");
    expect(ago(0, 60_000)).toBe("1 minute ago");
    expect(ago(0, 3 * 3_600_000)).toBe("3 hours ago");
  });
});
