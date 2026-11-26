import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ServerMessage } from "@copilot/protocol";
import type { StopReason, UiEvent } from "../src/api";
import { CallScreen, type CallProps } from "../src/CallScreen";
import { badge, initial, postCallFor, reduce, stopMessage, type SessionView } from "../src/session";
import { ToastProvider } from "../src/ui";
import { SETTINGS, settingsView } from "./fixtures";
import { ipc } from "./ipc";

const sid = "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210";
const server = (message: ServerMessage): UiEvent => ({ kind: "server", message });
const event = (state: SessionView, incoming: UiEvent) => reduce(state, { type: "event", event: incoming });
const live = () => event(initial, { kind: "connection", state: { state: "live", session_id: sid, resumed: false } });
const failure = () => server({ type: "error", seq: 5, code: "retention_unavailable", fatal: true, message: "A required write failed." });
const terminal = (state: SessionView, reason: StopReason) => {
  const connection = event(state, { kind: "connection", state: { state: "stopped", reason } });
  expect(connection.stopped).toBeNull();
  return event(connection, { kind: "stopped", reason });
};

describe("honest call completion", () => {
  it.each(["retention_failed", "end_unconfirmed"] as const)("keeps %s out of the successful post-call review", reason => {
    let state = live();
    state = event(state, server({ type: "question.detected", seq: 1, question_id: sid, text: "Where?", question_type: "small_talk", confidence: 1, speculative: false }));
    state = event(state, server({ type: "answer.delta", seq: 2, question_id: sid, pass: "fast", text: "A completed answer.", sequence: 0 }));
    state = event(state, server({ type: "answer.done", seq: 3, question_id: sid, pass: "fast", delta_count: 1, memory_refs: [], usage: { input_tokens: 1, output_tokens: 1, cached_tokens: 0 } }));
    if (reason === "retention_failed") state = event(state, failure());
    state = event(state, { kind: "closed", code: reason === "retention_failed" ? 4500 : null, reason: "" });
    state = terminal(state, reason);
    expect(state.running).toBe(false);
    expect(postCallFor(state)).toBeNull();
    expect(state.questions[0].fast.text).toBe("A completed answer.");
    expect(state.questions[0].fast.done).toBe(true);
    expect(state.questions[0].fast.error).toBeNull();
    expect(badge(state).text).toMatch(/saving/);
    expect(stopMessage(reason)).toContain("Check History");
    if (reason === "retention_failed") expect(state.log.some(line => line.includes("A required write failed."))).toBe(true);
  });

  it("preserves explicit error before close, but ignores stale server events after closure", () => {
    const before = event(live(), failure());
    expect(before.log.some(line => line.includes("retention_unavailable"))).toBe(true);
    const closed = event(before, { kind: "closed", code: 4500, reason: "" });
    expect(event(closed, server({ type: "error", seq: 6, code: "retention_unavailable", fatal: true, message: "stale" }))).toBe(closed);
    const stopped = terminal(closed, "retention_failed");
    expect(event(stopped, server({ type: "answer.delta", seq: 7, question_id: sid, pass: "fast", text: "late" }))).toBe(stopped);
  });

  it("keeps normal completion eligible only after the final native stopped event", () => {
    const closed = event(live(), { kind: "closed", code: 1000, reason: "" });
    const connection = event(closed, { kind: "connection", state: { state: "stopped", reason: "ended" } });
    expect(postCallFor(connection)).toBeNull();
    expect(postCallFor(event(connection, { kind: "stopped", reason: "ended" }))).toBe(sid);
    expect(stopMessage("ended")).toBeUndefined();
  });

  it("clears prior errors at the explicit new-call reset", () => {
    const stopped = terminal(event(live(), failure()), "retention_failed");
    const fresh = reduce(stopped, { type: "reset", running: true });
    expect(fresh.stopped).toBeNull();
    expect(fresh.log).toEqual([]);
    expect(fresh.sessionId).toBeNull();
  });

  it.each(["unauthorized", "outdated", "no_minutes"] as const)("keeps the existing %s label and review behavior", reason => {
    const state = terminal(live(), reason);
    expect(postCallFor(state)).toBeNull();
    expect(stopMessage(reason)).toBeUndefined();
  });
});

function routes() {
  ipc.handle("get_settings", () => settingsView());
  ipc.handle("live_status", () => ({ running: false, session_id: null, levels: null, stats: null }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
  ipc.api("GET", /^\/applications$/, () => ({ applications: [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [] }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/seats$/, () => ({ seats: [] }));
  ipc.api("GET", new RegExp("^/sessions/" + sid + "/seats$"), () => ({ seats: [], voice: true, screen: true }));
}
const props = (session: SessionView): CallProps => ({ session, tokenSet: true, environment: "cloud", release: true, signedInAs: "alex@example.com", settings: SETTINGS, careerOs: { connected: true, available: true }, locked: false, onCareerOsChanged() {}, onSettingsSaved() {}, onSettings() {}, onResumes() {}, onOpenLive: async () => {} });
const tree = (session: SessionView) => <ToastProvider><CallScreen {...props(session)} /></ToastProvider>;

it.each(["retention_failed", "end_unconfirmed"] as const)("renders the %s warning after a live call without loading success review", async reason => {
  routes();
  let state = live();
  const ui = render(tree(state));
  if (reason === "retention_failed") state = event(state, failure());
  state = event(state, { kind: "closed", code: reason === "retention_failed" ? 4500 : null, reason: "" });
  state = terminal(state, reason);
  await act(async () => ui.rerender(tree(state)));
  expect(await screen.findByText(reason === "retention_failed" ? "Call stopped; saving failed" : "Call stopped; saving not confirmed")).toBeVisible();
  expect(screen.getByText(stopMessage(reason)!)).toBeVisible();
  expect(screen.getByRole("heading", { name: "Start a call" })).toBeVisible();
  expect(ipc.apiCalls("GET").some(call => call.path === "/sessions/" + sid)).toBe(false);
});
