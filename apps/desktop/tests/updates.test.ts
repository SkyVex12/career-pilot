import { describe, expect, it } from "vitest";
import { ago, banner, throttled } from "../src/updates";

describe("the update banner", () => {
  it("shows nothing for an automatic update: it installs when no session runs", () => {
    expect(banner({ state: "ready", version: "0.1.2", notes: null, auto: true }, false)).toEqual({ kind: "none" });
    expect(banner({ state: "ready", version: "0.1.2", notes: null, auto: true }, true)).toEqual({ kind: "none" });
  });

  it("offers the button in ask mode, never during a session", () => {
    const idle = banner({ state: "ready", version: "0.1.2", notes: null, auto: false }, false);
    expect(idle).toMatchObject({ kind: "ask", canInstall: true });
    const busy = banner({ state: "ready", version: "0.1.2", notes: null, auto: false }, true);
    expect(busy).toMatchObject({ kind: "ask", canInstall: false });
    expect(busy.kind === "ask" && busy.text).toContain("after this session");
  });

  it("says when the app was updated, and keeps failures for the settings", () => {
    expect(banner({ state: "updated", version: "0.1.2" }, false)).toEqual({
      kind: "note",
      text: "Updated to 0.1.2.",
      dismissible: true,
    });
    expect(banner({ state: "failed", message: "network" }, false)).toEqual({ kind: "none" });
    expect(banner({ state: "outdated", message: "too old" }, false)).toMatchObject({ kind: "note", text: "too old" });
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
