import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";
import { captureMacScreen, nativeShares } from "../src/nativeScreen";

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
    const capture = captureMacScreen();
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
    const capture = captureMacScreen();
    const rejected = expect(capture).rejects.toThrow("permission revoked");
    fireEvent.click(await screen.findByRole("button", { name: "Display: Main display" }));
    await rejected;
    expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 });
  });
  it("keeps the chosen display identity and releases native and video capture once", async () => {
    const capture = captureMacScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Display: Main display" }));
    const [local] = await capture;
    expect(nativeShares.get(local.mediaStreamTrack)).toEqual(source);
    local.stop();
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("native_screen_stop", { token: 42 }));
    expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === "native_screen_stop")).toHaveLength(1);
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
});
