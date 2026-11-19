import { afterEach, describe, expect, it, vi } from "vitest";
import { normAccel, prettyKey, recordKey } from "../src/hotkeys";

afterEach(() => vi.unstubAllGlobals());
describe("Mac keyboard conventions", () => {
  it("records Command and Control separately, and shows the effective shortcut", () => {
    vi.stubGlobal("navigator", { platform: "MacIntel" });
    const event = { code: "KeyK", key: "k", ctrlKey: false, altKey: false, shiftKey: true, metaKey: true };
    expect(recordKey(event)).toEqual({ accel: "Shift+CommandOrControl+K" });
    expect(recordKey({ ...event, ctrlKey: true, metaKey: false })).toEqual({ accel: "Control+Shift+K" });
    expect(prettyKey("CommandOrControl+Shift+K")).toBe("Cmd+Shift+K");
    expect(normAccel("CommandOrControl+K")).toBe(normAccel("Command+K"));
    expect(normAccel("Control+K")).not.toBe(normAccel("Command+K"));
  });
  it("keeps Windows shortcut semantics", () => {
    vi.stubGlobal("navigator", { platform: "Win32" });
    expect(prettyKey("CommandOrControl+K")).toBe("Ctrl+K");
    expect(normAccel("CommandOrControl+K")).toBe(normAccel("Control+K"));
  });
  it("formats native key codes and modifier order without changing platform semantics", () => {
    vi.stubGlobal("navigator", { platform: "Win32" });
    expect(prettyKey("shift+control+KeyG")).toBe("Ctrl+Shift+G");
    expect(prettyKey("control+alt+Digit1")).toBe("Ctrl+Alt+1");
    expect(prettyKey("shift+control+ArrowUp")).toBe("Ctrl+Shift+↑");
    expect(prettyKey("Control+Shift+↑")).toBe("Ctrl+Shift+↑");
    expect(prettyKey("super+KeyG")).toBe("Win+G");

    vi.stubGlobal("navigator", { platform: "MacIntel" });
    expect(prettyKey("shift+super+KeyG")).toBe("Cmd+Shift+G");
    expect(prettyKey("shift+control+KeyG")).toBe("Ctrl+Shift+G");
    expect(prettyKey("commandorcontrol+Digit1")).toBe("Cmd+1");
    expect(prettyKey("Command+Shift+Left")).toBe("Cmd+Shift+←");
  });
});
