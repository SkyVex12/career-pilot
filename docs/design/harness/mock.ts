// The Rust core's commands and events, faked for the design harness (README.md). The URL picks
// the state, for example /?data=empty, /?call=midcall&compact=1#overlay:
//
//   data     full (default) | empty | loading | error | long | locked | signedout
//   call     none (default) | midcall | stream | ended | degraded | reconnecting | session_active
//   update   none (default) | downloading | ready
//   opacity  the overlay's opacity, 0.3 to 1        compact=1   click-through: ct=1
//   layout   split (default) | stacked              scale       text size, e.g. 1.3
//   dev=1    a development build (shows the environment and dev token settings)
//
// window.__harness.play() streams a new question's answers into a running call (call=stream).
import { mockIPC } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import type { Numbered, SettingsView, UiEvent } from "../../../apps/desktop/src/api";
import type { ServerMessage } from "../../../packages/protocol/src/generated";
import * as fx from "./fixtures";

const q = new URLSearchParams(location.search);
const data = q.get("data") ?? "full";
const call = q.get("call") ?? "none";
const long = data === "long";
const empty = data === "empty";

// The overlay's view prefs live in localStorage; set them before the app reads them.
try {
  localStorage.setItem("overlay.layout", q.get("layout") ?? "split");
  localStorage.setItem("overlay.textScale", q.get("scale") ?? "1");
  localStorage.setItem("overlay.compact", q.get("compact") === "1" ? "1" : "0");
} catch {
  // a private window: defaults
}

// --- The session's event stream --------------------------------------------------------------

let n = 0;
const ev = (event: UiEvent): Numbered => ({ n: n++, event });
const srv = (message: Omit<ServerMessage, "seq">) => ev({ kind: "server", message: { seq: n, ...message } as ServerMessage });
const final = (channel: "them" | "me", seg: number, text: string, at: number) =>
  srv({ type: "transcript.final", channel, seg_id: seg, text, start_ms: at, end_ms: at + 4000 } as never);
const detected = (id: string, text: string, type: string) =>
  srv({ type: "question.detected", question_id: id, text, question_type: type, confidence: 0.92, speculative: false } as never);
const delta = (id: string, pass: "fast" | "deep", text: string) => srv({ type: "answer.delta", question_id: id, pass, text } as never);
const done = (id: string, pass: "fast" | "deep", refs = fx.REFS) =>
  srv({ type: "answer.done", question_id: id, pass, memory_refs: refs, usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 }, source: "model" } as never);

function midcall(): Numbered[] {
  const out = [
    ev({ kind: "connection", state: { state: "live", session_id: "live-1", resumed: false } }),
    srv({ type: "session.ready", session_id: "live-1", server_time: new Date().toISOString(), limits: { max_minutes: 90 }, me_audio: true } as never),
    final("them", 1, "Thanks for joining. So, tell me about a time you handled a production incident", 2000),
    final("them", 2, "under pressure. What happened and what did you do?", 6000),
    detected("q1", "Tell me about a time you handled a production incident under pressure.", "behavioral"),
    delta("q1", "fast", fx.FAST1),
    done("q1", "fast"),
    delta("q1", "deep", fx.DEEP1),
    done("q1", "deep", []),
    final("me", 1, "Sure. Last spring our routing API started timing out during the evening peak.", 12000),
    final("them", 3, "Okay. How did you decide what to roll back first?", 60000),
    detected("q2", "How did you decide what to roll back first?", "technical"),
    delta("q2", "fast", fx.FAST2),
    done("q2", "fast", []),
  ];
  out.push(delta("q2", "deep", fx.DEEP2_PARTIAL));
  if (call === "stream") {
    out.push(done("q2", "deep", []));
    return out;
  }
  out.push(
    final("them", 4, "Makes sense. And how would you design the alerting so that does not happen again?", 95000),
    detected("q3", "How would you design the alerting so that does not happen again?", "system_design"),
    delta("q3", "fast", fx.FAST3_PARTIAL),
    srv({ type: "transcript.partial", channel: "them", seg_id: 5, text: "And keep in mind we have about forty services" } as never),
    ev({ kind: "vad", channel: "them", speaking: true, edge_ms: 0 }),
  );
  if (call === "degraded") out.push(srv({ type: "status", component: "llm", state: "degraded", message: "Answers are slow" } as never));
  if (call === "reconnecting") out.push(ev({ kind: "connection", state: { state: "reconnecting", attempt: 1, in_ms: 2000, close_code: 1006 } }));
  return out;
}

function snapshot(): [Numbered[], boolean, number] {
  n = 0;
  if (["midcall", "stream", "degraded", "reconnecting"].includes(call)) {
    const events = midcall();
    return [events, true, n];
  }
  if (call === "ended") {
    const events = [ev({ kind: "connection", state: { state: "live", session_id: "s1", resumed: false } }), ev({ kind: "stopped", reason: "ended" })];
    return [events, false, n];
  }
  if (call === "session_active") return [[ev({ kind: "stopped", reason: "session_active" })], false, n];
  return [[], false, 0];
}

/** Streams q4's fast answer word by word, then its deep answer, as the gateway does. */
async function play(wordMs = 45): Promise<void> {
  const send = (e: Numbered) => emit("copilot://event", e);
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  await send(final("them", 9, "Last one. How would you design the alerting so that does not happen again?", 120000));
  await send(detected("q4", "How would you design the alerting so that does not happen again?", "system_design"));
  for (const w of fx.FAST1.split(/(?<= )/)) {
    await send(delta("q4", "fast", w));
    await sleep(wordMs);
  }
  await send(done("q4", "fast"));
  for (const w of fx.DEEP1.split(/(?<= )/)) {
    await send(delta("q4", "deep", w));
    await sleep(wordMs / 2);
  }
  await send(done("q4", "deep", []));
}
(window as unknown as { __harness: unknown }).__harness = { play };

// --- Settings and the REST API -----------------------------------------------------------------

function settingsView(): SettingsView {
  const ready = data !== "signedout";
  const dev = q.get("dev") === "1";
  return {
    settings: {
      environment: dev ? "local" : "cloud",
      gateway_url: "ws://127.0.0.1:8081/v1/session",
      api_url: "http://127.0.0.1:8080/v1",
      cloud_gateway_url: "ws://stage.example.com/v1/session",
      cloud_api_url: "http://stage.example.com/v1",
      oauth: null,
      mic_device: null,
      output_device: null,
      echo_cancellation: true,
      hotkey: "CommandOrControl+Shift+Space",
      toggle_overlay_hotkey: "CommandOrControl+Shift+O",
      click_through_hotkey: "CommandOrControl+Shift+T",
      click_through_off_hotkey: "CommandOrControl+Shift+G",
      opacity_up_hotkey: "CommandOrControl+Shift+Up",
      opacity_down_hotkey: "CommandOrControl+Shift+Down",
      layouts: {},
      overlay_monitor: null,
      overlay_on_top: true,
      show_debug: false,
      last_session_id: null,
      profile: null,
      update_mode: "auto",
      last_version: null,
      pending_update: null,
    },
    version: "0.1.16",
    release: dev ? null : { stage: "stage0", api_url: "http://stage.example.com/v1" },
    dev_token_set: dev,
    signed_in_as: ready ? "alex@example.com" : null,
    ready,
    warnings: [],
    updates: { enabled: !dev, checked_at: Date.now() - 3_600_000 },
    hotkey_review: { invalid: [], conflicts: [] },
  };
}

const never = () => new Promise<never>(() => {});

function rest(method: string, path: string): unknown {
  if (data === "loading") return never();
  if (data === "error" && method === "GET" && !path.startsWith("/career-os/connection") && !path.startsWith("/profiles")) {
    throw "The API did not answer: connection refused (HTTP 502)";
  }
  const [p] = path.split("?");
  const m = (re: RegExp) => re.exec(p);
  if (method !== "GET") {
    if (m(/^\/employers\/[^/]+\/packs\/\w+$/)) return { mode: "standup", status: "ready", built_at: new Date().toISOString() };
    return {};
  }
  if (p === "/me") return { id: "u1", email: "alex@example.com", display_name: "Alex Rivera", plan: "beta" };
  if (p === "/usage") return { period: "October", live_seconds: 9000, live_minutes: empty ? 0 : 150, sessions: empty ? 0 : 4 };
  if (p === "/profiles") {
    return empty
      ? { items: [{ id: "me", name: "Alex Rivera", career_os_user_id: null, me: true }], career_os: null }
      : { items: [{ id: "me", name: "Alex Rivera", career_os_user_id: "u1", me: true }, { id: "p2", name: "Jordan Lee", career_os_user_id: "u2", me: false }], career_os: "admin" };
  }
  if (p === "/career-os/connection") {
    if (data === "locked") return { connected: false, available: true, required: true };
    if (empty) return { connected: false, available: true, required: false };
    return { connected: true, available: true, status: "active", user: { id: "u1", name: "Alex Rivera" }, account_type: "admin", connected_at: new Date(Date.now() - 864e6).toISOString() };
  }
  if (p === "/career-os/applications") return { items: fx.careerOsItems(), next_cursor: null };
  if (p === "/applications") return { applications: empty ? [] : fx.applications(long) };
  if (m(/^\/applications\/([^/]+)$/)) return fx.applicationDetail(m(/^\/applications\/([^/]+)$/)![1], long);
  if (p === "/interviews") return { interviews: empty ? [] : fx.interviews(long) };
  if (p === "/resumes") return empty ? { base: null, versions: [] } : fx.resumes(long);
  if (p === "/sessions") return { sessions: empty ? [] : fx.sessions(long), next_before: long ? "x" : null };
  if (p === "/sessions/live") return { sessions: [] };
  if (m(/^\/sessions\/([^/]+)\/replay$/)) return fx.replay(m(/^\/sessions\/([^/]+)\/replay$/)![1]);
  if (m(/^\/sessions\/([^/]+)\/transcript$/)) return { stored: true, segments: fx.replay("x").segments };
  if (m(/^\/sessions\/([^/]+)$/)) return fx.sessionDetail(m(/^\/sessions\/([^/]+)$/)![1]);
  if (p === "/employers") return { employers: empty ? [] : fx.employers(long) };
  if (m(/^\/employers\/[^/]+\/logs$/)) return { logs: fx.logs(long), next_before: null };
  if (m(/^\/employers\/[^/]+\/documents$/)) return { documents: fx.companyDocuments() };
  if (m(/^\/employers\/[^/]+\/memory$/)) return { memory: fx.memory(long) };
  if (m(/^\/employers\/[^/]+\/packs$/)) return { packs: [{ mode: "standup", status: "ready" }] };
  if (p === "/proposals") return { proposals: fx.proposals() };
  if (p === "/documents") return { documents: [], digest: null };
  if (p === "/jobs") return { jobs: [] };
  return {};
}

const update = q.get("update");

mockIPC(
  (cmd, args) => {
    const a = (args ?? {}) as Record<string, unknown>;
    switch (cmd) {
      case "get_settings":
        return settingsView();
      case "update_status":
        if (update === "downloading") return { state: "downloading", version: "0.1.17", percent: 42 };
        if (update === "ready") return { state: "ready", version: "0.1.17", notes: null, auto: true, install_in_s: 110 };
        return { state: "none" };
      case "list_devices":
        return {
          inputs: [{ id: "mic1", name: "Microphone (Jabra Evolve2 65)", is_default: true }, { id: "mic2", name: "Microphone Array (Realtek Audio)", is_default: false }],
          outputs: [{ id: "out1", name: "Headset Earphone (Jabra Evolve2 65)", is_default: true }, { id: "out2", name: "Speakers (Realtek Audio)", is_default: false }],
        };
      case "snapshot":
        return snapshot();
      case "live_status":
        return { running: call !== "none", session_id: "live-1", levels: { them_rms: 0.08, me_rms: 0.02, them_speech: 0.9, me_speech: 0.1, them_underruns: 0, me_underruns: 0 }, stats: null };
      case "overlay_prefs":
        return {
          opacity: Number(q.get("opacity") ?? 1),
          compact: q.get("compact") === "1",
          click_through: q.get("ct") === "1",
          on_top: true,
          hotkeys: { toggle_overlay: "CommandOrControl+Shift+O", click_through: "CommandOrControl+Shift+T", click_through_off: "CommandOrControl+Shift+G", opacity_up: "CommandOrControl+Shift+Up", opacity_down: "CommandOrControl+Shift+Down" },
        };
      case "api_call":
        return rest(String(a.method), String(a.path));
      default:
        // Every other command (start, end, hotkeys, feedback…) succeeds and does nothing.
        return null;
    }
  },
  { shouldMockEvents: true },
);

// The app's own entry, once the fake IPC is in place.
await import("../../../apps/desktop/src/main.tsx");
