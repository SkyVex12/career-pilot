// The audio test's display logic (AudioCheck.tsx): the meter's fill and what to tell the user,
// apart from React so it is tested on its own.
import type { AudioCheckMeter, AudioCheckResult } from "./api";

/** The meter's range: below FLOOR shows empty, at CEIL full. */
export const FLOOR_DB = -70;
export const CEIL_DB = -10;

/** The meter's fill, 0 to 1. */
export function meterFill(db: number): number {
  if (!Number.isFinite(db)) return 0;
  return Math.min(1, Math.max(0, (db - FLOOR_DB) / (CEIL_DB - FLOOR_DB)));
}

export type CheckState =
  | { state: "idle" }
  | { state: "running"; meter: AudioCheckMeter | null }
  | { state: "done"; result: AudioCheckResult }
  | { state: "failed"; message: string };

/** The line under the meters. */
export function checkLine(s: CheckState): string {
  switch (s.state) {
    case "idle":
      return "Checks the microphone and the call audio with the devices chosen above, before a call.";
    case "running":
      return s.meter?.phase === "tone"
        ? "Playing a short test sound on the call audio device…"
        : "Speak now: say a sentence as you would on the call.";
    case "done":
      return s.result.mic.ok && s.result.call.ok ? "Both work. You are ready for a call." : "Fix what is marked below, then test again.";
    case "failed":
      return s.message;
  }
}
