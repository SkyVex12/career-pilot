import { invoke } from "@tauri-apps/api/core";
import { LocalVideoTrack, Track } from "livekit-client";
import { createRoot } from "react-dom/client";
import { Button, Dialog } from "./ui";
import { Icon } from "./ui/Icon";
import "./capture.css";

type Source = { id: number; kind: "monitor" | "window"; title: string; monitor_id?: string };
type Frame = { sequence: number; width: number; height: number; data: string; error: string | null };
export const nativeShares = new WeakMap<MediaStreamTrack, Source & { error?: string; setQuality?: (height: number) => Promise<void> }>();
const cancelled = () => new DOMException("Share cancelled", "AbortError");

function choose(sources: Source[], signal?: AbortSignal): Promise<Source> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(cancelled()); return; }
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    let finished = false;
    const abort = () => finish();
    const finish = (source?: Source) => {
      if (finished) return;
      finished = true;
      signal?.removeEventListener("abort", abort);
      root.unmount(); host.remove();
      if (source) resolve(source); else reject(cancelled());
    };
    signal?.addEventListener("abort", abort, { once: true });
    root.render(<Dialog open onOpenChange={(open) => { if (!open) finish(); }} title="Choose what to share"
      description="Share a display for mouse and keyboard control, or a single window for viewing."
      footer={<Button onClick={() => finish()}>Cancel</Button>}>
      <div className="capture-sources">
        {sources.map((source) => <Button className="capture-source" key={`${source.kind}-${source.id}`} onClick={() => finish(source)}>
          <Icon name={source.kind === "monitor" ? "monitor" : "document"} size={22} /><span><strong>{source.title}</strong><small>{source.kind === "monitor" ? "View and remote control" : "View only"}</small></span>
        </Button>)}
        {!sources.length && <p>No screens or windows are available.</p>}
      </div>
    </Dialog>);
  });
}

/** Native capture provides one app-owned picker and stop control on Windows and macOS. Latest-frame polling
 * provides backpressure: a slow renderer never accumulates old video or delays remote input. */
export async function captureNativeScreen(signal?: AbortSignal): Promise<LocalVideoTrack[]> {
  if (signal?.aborted) throw cancelled();
  const canvas = document.createElement("canvas");
  if (typeof canvas.captureStream !== "function") throw new Error("This webview cannot publish screen video. Update the app and its webview runtime.");
  const source = await choose(await invoke<Source[]>("native_screen_sources"), signal);
  if (signal?.aborted) throw cancelled();
  const token = await invoke<number>("native_screen_start", { id: source.id, kind: source.kind });
  let stopped = false;
  let sequence = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let video: MediaStreamTrack | undefined;
  let local: LocalVideoTrack | undefined;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(timer);
    window.removeEventListener("pagehide", abort);
    signal?.removeEventListener("abort", abort);
    void invoke("native_screen_stop", { token }).catch(() => undefined);
  };
  const abort = () => { if (local) local.stop(); else stop(); };
  window.addEventListener("pagehide", abort, { once: true });
  signal?.addEventListener("abort", abort, { once: true });
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
    // Cancellation may arrive while native_screen_start is still in flight. Its
    // returned token must be released before rejecting; never lose that lease.
    if (signal?.aborted) { stop(); throw cancelled(); }
    if (!context) throw new Error("Could not create the screen video surface.");
    const deadline = Date.now() + 10_000;
    while (!(await draw())) {
      if (stopped) throw cancelled();
      if (Date.now() >= deadline) throw new Error("No screen frames arrived. Make sure the display or window is available and screen capture is allowed, then try again.");
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
    video = canvas.captureStream(30).getVideoTracks()[0];
    if (!video) throw new Error("Could not create the screen video track.");
    video.contentHint = "detail";
    nativeShares.set(video, { ...source, setQuality: (height) => {
      if (stopped) return Promise.reject(new Error("Screen sharing has stopped."));
      return invoke<void>("native_screen_quality", { token, height });
    } });
    local = new LocalVideoTrack(video, undefined, true);
    local.source = Track.Source.ScreenShare;
    const originalStop = local.stop.bind(local);
    let trackStopped = false;
    local.stop = () => {
      stop();
      if (!trackStopped) { trackStopped = true; originalStop(); }
    };
    const pump = async () => {
      try { await draw(); }
      catch (error) {
        if (stopped) return;
        nativeShares.set(video!, { ...source, error: error instanceof Error ? error.message : String(error) });
        local!.stop(); video!.dispatchEvent(new Event("ended"));
      }
      if (!stopped) timer = setTimeout(() => void pump(), 16);
    };
    void pump();
    return [local];
  } catch (error) { stop(); video?.stop(); throw error; }
}