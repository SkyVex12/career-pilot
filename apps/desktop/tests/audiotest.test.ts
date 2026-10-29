// The audio test's meter and messages (src/audiotest.ts).
import { describe, expect, it } from "vitest";
import { CEIL_DB, checkLine, FLOOR_DB, meterFill } from "../src/audiotest";

const verdict = (ok: boolean, message = "m") => ({ ok, message, peak_db: -20, sound_ms: 900 });

describe("audio check", () => {
  it("fills the meter between its floor and ceiling", () => {
    expect(meterFill(-100)).toBe(0);
    expect(meterFill(FLOOR_DB)).toBe(0);
    expect(meterFill(CEIL_DB)).toBe(1);
    expect(meterFill(0)).toBe(1);
    expect(meterFill((FLOOR_DB + CEIL_DB) / 2)).toBeCloseTo(0.5);
    expect(meterFill(Number.NaN)).toBe(0);
  });

  it("asks the user to speak, then says the test sound plays", () => {
    expect(checkLine({ state: "running", meter: null })).toMatch(/^Speak now/);
    expect(checkLine({ state: "running", meter: { phase: "speak", mic_db: -30, call_db: -90 } })).toMatch(/^Speak now/);
    expect(checkLine({ state: "running", meter: { phase: "tone", mic_db: -30, call_db: -20 } })).toMatch(/test sound/);
  });

  it("says ready only when both sides pass", () => {
    expect(checkLine({ state: "done", result: { mic: verdict(true), call: verdict(true) } })).toMatch(/ready for a call/);
    expect(checkLine({ state: "done", result: { mic: verdict(false), call: verdict(true) } })).toMatch(/Fix what is marked/);
    expect(checkLine({ state: "failed", message: "a session is already running" })).toBe("a session is already running");
  });
});
