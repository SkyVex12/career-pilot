// The audio test's display logic (AudioCheck.tsx): the meter's fill and what to tell the user,
// apart from React so it is tested on its own. Each device row has its own Test button: the
// microphone's listens while the user speaks, the call audio's plays a short tone.
import type { AudioCheckMeter, AudioCheckResult, AudioPart, AudioVerdict } from "./api";

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

/** The level the row's bar shows for its device. */
export function partLevel(part: AudioPart, meter: AudioCheckMeter | null): number {
  if (!meter) return -100;
  return part === "mic" ? meter.mic_db : meter.call_db;
}

export function partVerdict(part: AudioPart, result: AudioCheckResult): AudioVerdict {
  return part === "mic" ? result.mic : result.call;
}

/** The line under the row's bar; empty before the first test. */
export function partLine(part: AudioPart, s: CheckState): string {
  switch (s.state) {
    case "idle":
      return "";
    case "running":
      return part === "mic" ? "Speak now: say a sentence as you would on the call." : "Playing a short test sound on this device…";
    case "done":
      return partVerdict(part, s.result).message;
    case "failed":
      return s.message;
  }
}
