// Fictional native status fixtures. No ADB process, screen, microphone, or live API is used.
import type { BackgroundStatus, DetectionStatus } from "../../../apps/desktop/src/BackgroundSettings";

const preferences: BackgroundStatus["preferences"] = {
  launch_at_login: false, keep_running: true, suggestions_paused: false,
  call_suggestions: true, disabled_apps: [],
  hushed: { enabled: true, adb_path: "C:/Program Files/LDPlayer/adb.exe", serials: ["emulator-5554"] },
};

export function backgroundFixture(): BackgroundStatus {
  return {
    preferences: { ...preferences }, background_start: false, launch_at_login_supported: true,
    login_entry_present: false, call_detection_available: true, hushed_supported: true,
    hushed_adb_default: "C:/Program Files/LDPlayer/adb.exe",
    limitation: "Experimental Windows detection checks accessible call controls and microphone activity without recording. English controls only; real app calls are not yet certified. Notifications respect Focus settings.",
  };
}

export function productionCommand(command: string, args: Record<string, unknown>): unknown {
  if (command === "background_status") return backgroundFixture();
  if (command === "background_set_preferences") {
    Object.assign(preferences, args.preferences);
    return backgroundFixture();
  }
  if (command === "background_hushed_devices" || command === "background_hushed_connect") return ["emulator-5554", "emulator-5556"];
  if (command === "background_detection_status") {
    const status: DetectionStatus = {
      state: "watching_hushed", last_checked_unix_ms: Date.now(), error: null, presentation_error: null,
      pending: null, diagnostics: { microphone_active: false, microphone_apps: [], inspected_windows: 4,
        matched_calls: [], inspection_limited: false, notifications_allowed: true },
      hushed: { state: "idle", message: "Hushed is registered and ready. No active call.", connected: 1, monitored: 1 },
    };
    return status;
  }
  return undefined;
}
