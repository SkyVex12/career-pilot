import { invoke } from "@tauri-apps/api/core";
import { LocalVideoTrack, Track } from "livekit-client";
import { createRoot } from "react-dom/client";
import { Button, Dialog } from "./ui";

type Source = { id: number; kind: "monitor" | "window"; title: string };
type Frame = { sequence: number; width: number; height: number; data: string; error: string | null };
export const nativeShares = new WeakMap<MediaStreamTrack, Source & { error?: string }>();

function choose(sources: Source[]): Promise<Source> {
  return new Promise((resolve, reject) => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    const finish = (source?: Source) => {
      root.unmount(); host.remove();
      if (source) resolve(source); else reject(new DOMException("Share cancelled", "AbortError"));
    };
    root.render(<Dialog open onOpenChange={(open) => { if (!open) finish(); }} title="Share your screen"
      description="Choose a display for assistant control, or a window for viewing. Copilot windows are excluded from this share."
      footer={<Button onClick={() => finish()}>Cancel</Button>}>
      <div className="ui-stack">
        {sources.map((source) => <Button key={`${source.kind}-${source.id}`} onClick={() => finish(source)}>
          {source.kind === "monitor" ? "Display" : "Window"}: {source.title}
        </Button>)}
        {!sources.length && <p>No screens or windows are available.</p>}
      </div>
    </Dialog>);
  });
}

/** Native capture avoids WKWebView's unsupported getDisplayMedia path. Latest-frame polling
 * provides backpressure: a slow renderer never accumulates old video or delays remote input. */
export async function captureMacScreen(): Promise<LocalVideoTrack[]> {
  const canvas = document.createElement("canvas");
  if (typeof canvas.captureStream !== "function") throw new Error("Update macOS to enable screen sharing in this app.");
  const source = await choose(await invoke<Source[]>("native_screen_sources"));
  const token = await invoke<number>("native_screen_start", { id: source.id, kind: source.kind });
  let stopped = false;
  let sequence = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let video: MediaStreamTrack | undefined;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(timer);
    window.removeEventListener("pagehide", stop);
    void invoke("native_screen_stop", { token }).catch(() => undefined);
  };
  window.addEventListener("pagehide", stop, { once: true });
  const context = canvas.getContext("2d", { alpha: false });
  async function draw(): Promise<boolean> {
    const frame = await invoke<Frame | null>("native_screen_frame", { token, after: sequence });
    if (stopped || !frame) return false;
    if (frame.error) throw new Error(frame.error);
    if (!frame.width || !frame.height || !frame.data) return false;
    const image = new Image();
    image.src = `data:image/jpeg;base64,${frame.data}`;
    await image.decode();
    if (stopped) return false;
    if (canvas.width !== frame.width || canvas.height !== frame.height) {
      canvas.width = frame.width; canvas.height = frame.height;
    }
    context!.drawImage(image, 0, 0);
    sequence = frame.sequence;
    return true;
  }
  try {
    if (!context) throw new Error("Could not create the screen video surface.");
    const deadline = Date.now() + 10_000;
    while (!(await draw())) {
      if (stopped || Date.now() >= deadline) throw new Error("No screen frames arrived. Check Screen Recording permission and try again.");
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
    video = canvas.captureStream(60).getVideoTracks()[0];
    if (!video) throw new Error("Could not create the screen video track.");
    nativeShares.set(video, source);
    const local = new LocalVideoTrack(video, undefined, true);
    local.source = Track.Source.ScreenShare;
    const originalStop = local.stop.bind(local);
    local.stop = () => { stop(); originalStop(); };
    const pump = async () => {
      try { await draw(); }
      catch (error) {
        nativeShares.set(video!, { ...source, error: error instanceof Error ? error.message : String(error) });
        stop(); video!.dispatchEvent(new Event("ended"));
      }
      if (!stopped) timer = setTimeout(() => void pump(), 16);
    };
    void pump();
    return [local];
  } catch (error) { stop(); video?.stop(); throw error; }
}
