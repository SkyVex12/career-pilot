import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";
import { captureNativeScreen, nativeShares, NATIVE_SCREEN_SIZE } from "../src/nativeScreen";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("livekit-client", () => ({
  Track: { Source: { ScreenShare: "screen_share" } },
  LocalVideoTrack: class {
    source = "unknown";
    constructor(public mediaStreamTrack: MediaStreamTrack) {}
    stop() { this.mediaStreamTrack.stop(); }
  },
}));
const source = { id: 7, kind: "monitor", title: "Main display" };
const frame = { sequence: 1, width: 1920, height: 1080, data: "jpeg", error: null };
let track: MediaStreamTrack;
beforeEach(() => {
  vi.clearAllMocks();
  track = Object.assign(new EventTarget(), { stop: vi.fn() }) as unknown as MediaStreamTrack;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  Object.defineProperty(HTMLCanvasElement.prototype, "captureStream", { configurable: true, value: () => ({ getVideoTracks: () => [track] }) });
  vi.stubGlobal("Image", class { src = ""; decode = async () => undefined; });
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === "native_screen_sources") return [source];
    if (command === "native_screen_start") return 42;
    if (command === "native_screen_frame") return (args as { after: number }).after === 0 ? frame : null;
    return undefined;
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("native screen stream ownership", () => {
  it("never starts capture when the picker is cancelled", async () => {
    const capture = captureNativeScreen();
    const rejected = expect(capture).rejects.toMatchObject({ name: "AbortError" });
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await rejected;
    expect(invoke).not.toHaveBeenCalledWith("native_screen_start", expect.anything());
  });
  it("stops the native stream if the first frame fails", async () => {
    vi.mocked(invoke).mockImplementation(async (command) => {
      if (command === "native_screen_sources") return [source];
      if (command === "native_screen_start") return 42;
      if (command === "native_screen_frame") throw new Error("Screen Recording permission revoked");
      return undefined;
    });
    const capture = captureNativeScreen();
    const rejected = expect(capture).rejects.toThrow("permission revoked");
    fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
    await rejected;
    expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 });
  });
  it("keeps the chosen display identity and releases native and video capture once", async () => {
    const capture = captureNativeScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
    const [local] = await capture;
    expect(nativeShares.get(local.mediaStreamTrack)).toMatchObject(source);
    await nativeShares.get(local.mediaStreamTrack)?.setQuality?.(2160);
    expect(invoke).toHaveBeenCalledWith("native_screen_quality", { token: 42, height: 2160 });
    local.stop();
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 }));
    expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === "native_screen_stop")).toHaveLength(1);
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
});

it("closes an aborted source picker without starting capture", async () => {
  const controller = new AbortController();
  const capture = captureNativeScreen(controller.signal);
  const rejected = expect(capture).rejects.toMatchObject({ name: "AbortError" });
  await screen.findByRole("dialog", { name: "Choose what to share" });
  controller.abort();
  await rejected;
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalledWith("native_screen_start", expect.anything());
});

it("releases a token returned after cancellation during native startup", async () => {
  let finishStart!: (token: number) => void;
  vi.mocked(invoke).mockImplementation(async command => {
    if (command === "native_screen_sources") return [source];
    if (command === "native_screen_start") return new Promise<number>(resolve => { finishStart = resolve; });
    return undefined;
  });
  const controller = new AbortController();
  const capture = captureNativeScreen(controller.signal);
  const rejected = expect(capture).rejects.toMatchObject({ name: "AbortError" });
  fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
  await waitFor(() => expect(finishStart).toBeDefined());
  controller.abort();
  finishStart(42);
  await rejected;
  expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 });
  expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === "native_screen_frame")).toHaveLength(0);
});

it("stops native capture immediately when the first frame is cancelled", async () => {
  let finishFrame!: () => void;
  vi.mocked(invoke).mockImplementation(async command => {
    if (command === "native_screen_sources") return [source];
    if (command === "native_screen_start") return 42;
    if (command === "native_screen_frame") return new Promise<null>(resolve => { finishFrame = () => resolve(null); });
    return undefined;
  });
  const controller = new AbortController();
  const capture = captureNativeScreen(controller.signal);
  const rejected = expect(capture).rejects.toMatchObject({ name: "AbortError" });
  fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
  await waitFor(() => expect(finishFrame).toBeDefined());
  controller.abort();
  expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 });
  finishFrame();
  await rejected;
});

it("cancels a running native stream and video track exactly once", async () => {
  const controller = new AbortController();
  const capture = captureNativeScreen(controller.signal);
  fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
  const [local] = await capture;
  controller.abort();
  local.stop();
  expect(track.stop).toHaveBeenCalledTimes(1);
  expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === "native_screen_stop")).toHaveLength(1);
});

it("stops the video track on a later native frame failure even without an ended listener", async () => {
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === "native_screen_sources") return [source];
    if (command === "native_screen_start") return 42;
    if (command === "native_screen_frame") return (args as { after: number }).after === 0 ? frame : { ...frame, error: "Display disconnected" };
    return undefined;
  });
  const capture = captureNativeScreen();
  fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
  const [local] = await capture;
  await waitFor(() => expect(track.stop).toHaveBeenCalledTimes(1));
  expect(nativeShares.get(local.mediaStreamTrack)?.error).toBe("Display disconnected");
  expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 });
});

it("publishes dimensions only after the resized native frame has been decoded and drawn", async () => {
  let latest = frame;
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === "native_screen_sources") return [source];
    if (command === "native_screen_start") return 42;
    if (command === "native_screen_frame") return (args as { after: number }).after < latest.sequence ? latest : null;
    return undefined;
  });
  const capture = captureNativeScreen();
  fireEvent.click(await screen.findByRole("button", { name: "Main display View and remote control" }));
  const [local] = await capture;
  const resize = vi.fn();
  track.addEventListener(NATIVE_SCREEN_SIZE, resize);
  try {
    await nativeShares.get(track)?.setQuality?.(2160);
    expect(resize).not.toHaveBeenCalled();
    expect(nativeShares.get(track)?.dimensions).toEqual({ width: 1920, height: 1080 });
    latest = { ...frame, sequence: 2, width: 3840, height: 2160 };
    await waitFor(() => expect(resize).toHaveBeenCalledTimes(1));
    expect(nativeShares.get(track)?.dimensions).toEqual({ width: 3840, height: 2160 });
    latest = { ...latest, sequence: 3 };
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("native_screen_frame", { token: 42, after: 3 }));
    expect(resize).toHaveBeenCalledTimes(1);
  } finally { local.stop(); }
});
