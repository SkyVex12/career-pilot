import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { BackgroundSettings } from "../src/BackgroundSettings";
import { HushedDetectionSettings, type HushedPreferences } from "../src/HushedDetectionSettings";
import { ipc } from "./ipc";

const adb = "C:\\LDPlayer\\LDPlayer9\\adb.exe";
const preferences: HushedPreferences = { enabled: false, adb_path: adb, serials: [] };
function form(overrides: Partial<Parameters<typeof HushedDetectionSettings>[0]> = {}) {
  const onSave = vi.fn(async () => true);
  const view = render(<HushedDetectionSettings preferences={preferences} supported disabled={false} onSave={onSave} {...overrides} />);
  fireEvent.click(screen.getByText("Hushed in LDPlayer"));
  return { ...view, onSave };
}

it("keeps opt-in edits local until an explicitly validated save", async () => {
  const { onSave } = form();
  expect(ipc.calls).toHaveLength(0);
  fireEvent.click(screen.getByRole("switch", { name: "Detect Hushed calls" }));
  fireEvent.change(screen.getByLabelText("LDPlayer ADB executable"), { target: { value: `${adb}x` } });
  expect(ipc.calls).toHaveLength(0);
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save Hushed detection" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("select at least one");
  expect(onSave).not.toHaveBeenCalled();
});

it("finds devices only on demand, limits selection to four, and saves the explicit selection", async () => {
  ipc.handle("background_hushed_devices", () => ["emulator-5554", "emulator-5556", "emulator-5558", "emulator-5560", "emulator-5562"]);
  const { onSave } = form();
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  await screen.findByRole("checkbox", { name: "emulator-5554" });
  expect(ipc.calls).toEqual([{ cmd: "background_hushed_devices", args: { adbPath: adb } }]);
  expect(onSave).not.toHaveBeenCalled();
  for (const serial of ["emulator-5554", "emulator-5556", "emulator-5558", "emulator-5560"]) fireEvent.click(screen.getByRole("checkbox", { name: serial }));
  expect(screen.getByRole("checkbox", { name: "emulator-5562" })).toBeDisabled();
  fireEvent.click(screen.getByRole("switch", { name: "Detect Hushed calls" }));
  fireEvent.click(screen.getByRole("button", { name: "Save Hushed detection" }));
  expect(await screen.findByRole("status")).toHaveTextContent("settings saved");
  expect(onSave).toHaveBeenCalledExactlyOnceWith({ enabled: true, adb_path: adb, serials: ["emulator-5554", "emulator-5556", "emulator-5558", "emulator-5560"] });
});

it("connects only an explicit valid loopback endpoint and does not auto-select it", async () => {
  ipc.handle("background_hushed_connect", () => ["127.0.0.1:5555"]);
  const { onSave } = form();
  fireEvent.click(screen.getByText("Connect a local emulator"));
  for (const address of ["192.168.1.2:5555", "localhost:5555", "[::1]:5555", "127.0.0.1:0", "127.0.0.1:05555", "127.0.0.1:65536", "127.0.0.1:5555 extra"]) {
    fireEvent.change(screen.getByLabelText("Local emulator address"), { target: { value: address } });
    fireEvent.click(screen.getByRole("button", { name: "Connect local emulator" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use 127.0.0.1");
  }
  expect(ipc.calls).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Local emulator address"), { target: { value: "127.0.0.1:5555" } });
  fireEvent.click(screen.getByRole("button", { name: "Connect local emulator" }));
  expect(await screen.findByRole("checkbox", { name: "127.0.0.1:5555" })).not.toBeChecked();
  expect(ipc.calls).toEqual([{ cmd: "background_hushed_connect", args: { adbPath: adb, serial: "127.0.0.1:5555" } }]);
  expect(onSave).not.toHaveBeenCalled();
});

it("shows discovery errors and permits retry while retaining saved offline devices", async () => {
  ipc.handle("background_hushed_devices", () => { throw new Error("ADB is unavailable"); });
  form({ preferences: { ...preferences, enabled: true, serials: ["emulator-5554"] } });
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("ADB is unavailable");
  ipc.handle("background_hushed_devices", () => []);
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  expect(await screen.findByRole("status")).toHaveTextContent("No connected emulators found");
  expect(screen.getByRole("checkbox", { name: "emulator-5554" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "emulator-5554" })).toHaveAccessibleDescription("Saved selection; not currently connected.");
});

it("discards cancelled discovery results after a newer scan completes", async () => {
  let finish!: (devices: string[]) => void;
  ipc.handle("background_hushed_devices", () => new Promise(resolve => { finish = resolve; }));
  form();
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  ipc.handle("background_hushed_devices", () => ["emulator-5556"]);
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  await screen.findByRole("checkbox", { name: "emulator-5556" });
  await act(async () => { finish(["emulator-5554"]); });
  expect(screen.queryByRole("checkbox", { name: "emulator-5554" })).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "emulator-5556" })).toBeInTheDocument();
});

it("does not save or perform another command when an unmounted scan completes", async () => {
  let finish!: (devices: string[]) => void;
  ipc.handle("background_hushed_devices", () => new Promise(resolve => { finish = resolve; }));
  const { unmount, onSave } = form();
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  unmount();
  await act(async () => { finish(["emulator-5554"]); });
  expect(ipc.calls).toHaveLength(1);
  expect(onSave).not.toHaveBeenCalled();
});

it("does not report success after a rejected preference save", async () => {
  const onSave = vi.fn(async () => false);
  form({ preferences: { ...preferences, enabled: true, serials: ["emulator-5554"] }, onSave });
  fireEvent.click(screen.getByRole("switch", { name: "Detect Hushed calls" }));
  fireEvent.click(screen.getByRole("button", { name: "Save Hushed detection" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("settings were not saved");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it.each(["immediate", "deferred"])("retains unsaved Hushed edits when unrelated background preferences are saved (%s response)", async response => {
  const background = {
    preferences: { launch_at_login: false, keep_running: true, suggestions_paused: false, call_suggestions: true, hushed: preferences },
    call_detection_available: true, hushed_supported: true, hushed_adb_default: adb, limitation: "",
  };
  ipc.handle("background_status", () => background);
  let finishSave!: () => void;
  ipc.handle("background_set_preferences", args => {
    const next = { ...background, preferences: JSON.parse(JSON.stringify(args.preferences)) };
    if (response === "immediate") return next;
    return new Promise(resolve => { finishSave = () => resolve(next); });
  });
  // Settle the asynchronous status load and draft initialization before editing.
  await act(async () => { render(<BackgroundSettings />); });
  fireEvent.click(screen.getByText("Hushed in LDPlayer"));
  const detect = screen.getByRole("switch", { name: "Detect Hushed calls" });
  const executable = screen.getByLabelText("LDPlayer ADB executable");
  const pause = screen.getByRole("switch", { name: "Pause call suggestions" });
  fireEvent.click(detect);
  expect(detect).toBeChecked();
  fireEvent.change(executable, { target: { value: "D:\\LDPlayer\\adb.exe" } });
  expect(executable).toHaveValue("D:\\LDPlayer\\adb.exe");
  fireEvent.click(pause);
  expect(detect).toBeChecked();
  expect(executable).toHaveValue("D:\\LDPlayer\\adb.exe");
  if (response === "deferred") {
    expect(pause).toBeDisabled();
    expect(detect).toBeDisabled();
  }
  expect(ipc.calls.find(call => call.cmd === "background_set_preferences")?.args).toEqual({ preferences: { ...background.preferences, suggestions_paused: true } });
  await act(async () => { if (response === "deferred") finishSave(); });
  expect(screen.getByRole("switch", { name: "Pause call suggestions" })).toBeChecked();
  expect(screen.getByRole("switch", { name: "Pause call suggestions" })).toBeEnabled();
  expect(screen.getByRole("switch", { name: "Detect Hushed calls" })).toBeChecked();
  expect(screen.getByRole("switch", { name: "Detect Hushed calls" })).toBeEnabled();
  expect(screen.getByLabelText("LDPlayer ADB executable")).toHaveValue("D:\\LDPlayer\\adb.exe");
});

it("explains Windows support and shows separate Hushed detection diagnostics", () => {
  form({ supported: false, detection: { state: "unsupported", message: "This platform is unsupported", monitored: 1, connected: 0 } });
  expect(screen.getByText("Hushed detection in LDPlayer is available on Windows only.")).toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(screen.getByRole("status", { name: "Hushed detection status" })).toHaveTextContent("This platform is unsupported. 1 selected, 0 connected");
  expect(ipc.calls).toHaveLength(0);
});

it("integrates an explicit Hushed save without enabling global suggestions or changing application exclusions", async () => {
  const background = {
    preferences: { launch_at_login: false, keep_running: true, suggestions_paused: true, call_suggestions: false, disabled_apps: ["LDPlayer"], hushed: preferences },
    call_detection_available: true, hushed_supported: true, hushed_adb_default: adb, limitation: "",
  };
  ipc.handle("background_status", () => background);
  ipc.handle("background_hushed_devices", () => ["emulator-5554"]);
  ipc.handle("background_set_preferences", args => ({ ...background, preferences: args.preferences }));
  ipc.handle("background_detection_status", () => ({ state: "paused", hushed: { state: "offline", message: "Selected emulator is offline", monitored: 1, connected: 0 } }));
  render(<BackgroundSettings />);
  fireEvent.click(await screen.findByText("Hushed in LDPlayer"));
  fireEvent.click(screen.getByRole("button", { name: "Find emulators" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: "emulator-5554" }));
  fireEvent.click(screen.getByRole("switch", { name: "Detect Hushed calls" }));
  fireEvent.click(screen.getByRole("button", { name: "Save Hushed detection" }));
  expect(await screen.findByText(/Hushed detection settings saved/)).toBeInTheDocument();
  expect(ipc.calls.find(call => call.cmd === "background_set_preferences")?.args).toEqual({ preferences: {
    ...background.preferences, hushed: { enabled: true, adb_path: adb, serials: ["emulator-5554"] },
  } });
  fireEvent.click(screen.getByRole("button", { name: "Check detection" }));
  const status = await screen.findByRole("status", { name: "Hushed detection status" });
  expect(status).toHaveTextContent("Selected emulator is offline");
  expect(within(status).queryByRole("button")).not.toBeInTheDocument();
});
