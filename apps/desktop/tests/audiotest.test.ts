// The audio test's meter and messages (src/audiotest.ts): one Test button per device row.
import { describe, expect, it } from "vitest";
import { CEIL_DB, FLOOR_DB, meterFill, partLevel, partLine } from "../src/audiotest";

const verdict = (ok: boolean, message: string) => ({ ok, message, peak_db: -20, sound_ms: 900 });
const result = { mic: verdict(true, "Your voice comes through."), call: verdict(false, "Nothing was heard from this output.") };

describe("audio check", () => {
  it("fills the meter between its floor and ceiling", () => {
    expect(meterFill(-100)).toBe(0);
    expect(meterFill(FLOOR_DB)).toBe(0);
    expect(meterFill(CEIL_DB)).toBe(1);
    expect(meterFill(0)).toBe(1);
    expect(meterFill((FLOOR_DB + CEIL_DB) / 2)).toBeCloseTo(0.5);
    expect(meterFill(Number.NaN)).toBe(0);
  });

  it("shows each row's own level", () => {
    const meter = { phase: "speak" as const, mic_db: -30, call_db: -90 };
    expect(partLevel("mic", meter)).toBe(-30);
    expect(partLevel("call", meter)).toBe(-90);
    expect(partLevel("mic", null)).toBe(-100);
  });

  it("says nothing before a test, then what to do, then that row's verdict", () => {
    expect(partLine("mic", { state: "idle" })).toBe("");
    expect(partLine("mic", { state: "running", meter: null })).toMatch(/^Speak now/);
    expect(partLine("call", { state: "running", meter: null })).toMatch(/test sound/);
    expect(partLine("mic", { state: "done", result })).toBe("Your voice comes through.");
    expect(partLine("call", { state: "done", result })).toBe("Nothing was heard from this output.");
    expect(partLine("call", { state: "failed", message: "a session is already running" })).toBe("a session is already running");
  });
});
