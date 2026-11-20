import { afterEach, expect, it, vi } from "vitest";
import { PracticeAudio, prepareForRealCall, registerPracticePause } from "../src/practiceAudio";
afterEach(() => vi.unstubAllGlobals());
function devices(getUserMedia: () => Promise<MediaStream>) { vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } }); vi.stubGlobal("MediaRecorder", class { static isTypeSupported() { return true; } state = "inactive"; ondataavailable: unknown; onstop: unknown; start() { this.state = "recording"; } stop() { this.state = "inactive"; } }); }
it("requests only microphone and releases every track on stop", async () => {
  const stop = vi.fn(); const track = { stop, onended: null }; const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const getUserMedia = vi.fn().mockResolvedValue(stream); devices(getUserMedia);
  const audio = new PracticeAudio(); expect(await audio.start()).toBe(true);
  expect(getUserMedia).toHaveBeenCalledWith({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
  audio.stop(); expect(stop).toHaveBeenCalledTimes(1);
});
it("discards late microphone permission after cancellation without recording", async () => {
  let resolve!: (s: MediaStream) => void; const stop = vi.fn(); devices(() => new Promise(r => { resolve = r; }));
  const audio = new PracticeAudio(); const opening = audio.start(); audio.stop(); resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream);
  expect(await opening).toBe(false); expect(stop).toHaveBeenCalledOnce();
});
it("awaits practice pause before allowing a real call and propagates failure", async () => {
  let resolve!: () => void; const off = registerPracticePause(() => new Promise(r => { resolve = r; }));
  let ready = false; const call = prepareForRealCall().then(() => { ready = true; }); await Promise.resolve(); expect(ready).toBe(false); resolve(); await call; expect(ready).toBe(true); off();
  const remove = registerPracticePause(async () => { throw new Error("pause unavailable"); }); await expect(prepareForRealCall()).rejects.toThrow("pause unavailable"); remove();
});
