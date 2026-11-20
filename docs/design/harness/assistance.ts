// Local-only media fixture for visual QA. No screen, microphone, network or remote input is used.
import { RoomEvent, Track, type Room, type RemoteTrack } from "livekit-client";

export async function installAssistanceFixture(state: string) {
  const { guestDeps, TOPIC } = await import("../../../apps/desktop/src/screen");
  guestDeps.token = async () => ({ url: "fixture://local", token: "local-only", control: true });
  guestDeps.newRoom = () => {
    const events = new Map<string, Array<(...args: unknown[]) => void>>();
    let closed = false;
    const fire = (name: string, ...args: unknown[]) => events.get(name)?.forEach(fn => fn(...args));
    const canvas = document.createElement("canvas");
    canvas.width = 1440; canvas.height = 900;
    const draw = () => {
      const c = canvas.getContext("2d")!;
      c.fillStyle = "#132333"; c.fillRect(0, 0, 1440, 900);
      c.fillStyle = "#20262e"; c.fillRect(90, 72, 1260, 744);
      c.fillStyle = "#313842"; c.fillRect(90, 72, 1260, 42);
      c.font = "16px Segoe UI"; c.fillStyle = "#dae2ec";
      c.fillText("Shared laptop · local design fixture", 112, 99);
      c.fillStyle = "#171d25"; c.fillRect(90, 114, 240, 702);
      c.font = "18px Segoe UI"; c.fillStyle = "#b8c5d5";
      ["EXPLORER", "src", "  app.ts", "  routes.ts", "  components", "package.json"].forEach((t,i) => c.fillText(t, 118, 154+i*40));
      c.fillStyle = "#d7e0ed"; c.font = "21px Consolas";
      ["// Customer search", "", "export async function search(query: string) {", "  const matches = await customers.find({", "    name: query.trim(),", "    active: true", "  });", "", "  return matches.map(customer => ({", "    id: customer.id,", "    name: customer.name", "  }));", "}"].forEach((t,i) => c.fillText(t, 374, 178+i*39));
      c.fillStyle = "#2d455d"; c.fillRect(0, 850, 1440, 50);
      c.fillStyle = "#e5ebf4"; c.font = "16px Segoe UI"; c.fillText("Fixture desktop — no live screen capture", 38, 881);
    };
    draw();
    const stream = canvas.captureStream(5);
    const track = {
      kind: Track.Kind.Video,
      attach: (el: HTMLVideoElement) => { el.srcObject = stream; void el.play(); return el; },
      detach: (el?: HTMLVideoElement) => { if (el) el.srcObject = null; return []; },
      setPlayoutDelay: () => {},
      getRTCStatsReport: async () => new Map(),
    } as unknown as RemoteTrack;
    const control = (on: boolean) => fire(RoomEvent.DataReceived, new TextEncoder().encode(JSON.stringify({ on })), { identity: "host" }, undefined, TOPIC.control);
    return {
      on(name: string, fn: (...args: unknown[]) => void) { const list=events.get(name) ?? []; list.push(fn); events.set(name,list); return this; },
      async connect() {
        if (state === "error") throw new Error("Connection interrupted. Reconnect to continue viewing.");
        if (closed || state === "waiting") return;
        fire(RoomEvent.TrackSubscribed, track, {}, { identity: "host" });
        if (state === "control") control(true);
      },
      async disconnect() { closed=true; stream.getTracks().forEach(t=>t.stop()); },
      localParticipant: { async setMicrophoneEnabled() {}, async publishData(_data: Uint8Array, options: { topic: string }) {
        if (options.topic === TOPIC.ask) control(true);
        if (options.topic === TOPIC.release) control(false);
      } },
    } as unknown as Room;
  };
  const { browserDeps } = await import("../../../apps/desktop/src/voice");
  browserDeps.guestToken = async () => ({ url: "fixture://local", token: "local-only" });
  browserDeps.newRoom = guestDeps.newRoom;
}