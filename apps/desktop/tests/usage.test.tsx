import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UsageTotals } from "../src/api";
import { AccountUsage, LiveUsage, connectedDuration } from "../src/Usage";
import { initial, type SessionView } from "../src/session";
import { DataActivity } from "../src/usePolling";
import { ipc } from "./ipc";

const live = (id = "s1"): SessionView => ({ ...initial, sessionId: id, running: true, connection: { state: "live", session_id: id, resumed: false } });
const totals = (sessionId: string | null, seconds = 60, total = 600): UsageTotals => ({ tracking_only: true, measurement: "connected-v1", as_of: "2026-11-23T12:00:00Z", total_seconds: total, total_minutes: total / 60, total_sessions: 2, session_id: sessionId, session_seconds: sessionId ? seconds : null, session_minutes: sessionId ? seconds / 60 : null, last_connected_at: "2026-11-23T11:59:50Z" });
const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

afterEach(() => vi.useRealTimers());

describe("confirmed consumed minutes", () => {
  it("updates during silence, excludes reconnect downtime and never adds the call twice", async () => {
    vi.useFakeTimers();
    let recorded = totals("s1");
    ipc.api("GET", /^\/usage\/totals$/, () => recorded);
    const view = render(<LiveUsage session={live()} />);
    await tick();
    const usage = screen.getByRole("group", { name: "This call's consumed minutes" });
    expect(usage).toHaveTextContent("1.0 minutes used this call · 10.0 total minutes used");
    await tick(9_000);
    expect(usage).toHaveTextContent("1.0 minutes used this call");
    recorded = totals("s1", 90, 630);
    await tick(1_000);
    expect(usage).toHaveTextContent("1.5 minutes used this call · 10.5 total minutes used");
    view.rerender(<LiveUsage session={{ ...live(), connection: { state: "reconnecting", attempt: 1, in_ms: 1000, close_code: null } }} />);
    await tick(60_000);
    expect(usage).toHaveTextContent("1.5 minutes used this call · 10.5 total minutes used");
    expect(usage).toHaveTextContent("reconnect time is excluded");
    expect(ipc.apiCalls().every(call => call.path === "/usage/totals?session_id=s1")).toBe(true);
    expect(ipc.calls.filter(call => call.cmd === "api_call").every(call => call.args.refresh === true && call.args.wake === false)).toBe(true);
    view.unmount();
  });

  it("refreshes final confirmed time after end and pauses polling when hidden", async () => {
    vi.useFakeTimers();
    let recorded = totals("s1");
    ipc.api("GET", /^\/usage\/totals$/, () => recorded);
    const view = render(<DataActivity.Provider value={true}><LiveUsage session={live()} compact /></DataActivity.Provider>);
    await tick();
    expect(screen.getByRole("timer", { name: "Call duration" })).toHaveTextContent("01:00");
    recorded = totals("s1", 120, 660);
    const ended: SessionView = { ...live(), running: false, connection: { state: "stopped", reason: "ended" }, stopped: "ended" };
    view.rerender(<DataActivity.Provider value={true}><LiveUsage session={ended} compact /></DataActivity.Provider>);
    await tick();
    expect(screen.getByRole("timer", { name: "Call duration" })).toHaveTextContent("02:00");
    const before = ipc.apiCalls().length;
    view.rerender(<DataActivity.Provider value={false}><LiveUsage session={ended} compact /></DataActivity.Provider>);
    await tick(90_000);
    expect(ipc.apiCalls()).toHaveLength(before);
    expect(screen.getByRole("timer", { name: "Call duration" })).toHaveTextContent("02:00");
    view.unmount();
  });

  it("discards a delayed response for the previous call", async () => {
    vi.useFakeTimers();
    let finish!: (value: UsageTotals) => void;
    ipc.api("GET", /^\/usage\/totals$/, path => path.includes("session_id=s1") ? new Promise<UsageTotals>(resolve => { finish = resolve; }) : totals("s2", 30, 630));
    const view = render(<LiveUsage session={live()} />);
    await tick();
    view.rerender(<LiveUsage session={live("s2")} />);
    await act(async () => { finish(totals("s1", 300)); });
    expect(screen.getByRole("group")).not.toHaveTextContent("5.0 minutes");
    await tick(10_000);
    expect(screen.getByRole("group")).toHaveTextContent("0.5 minutes used this call");
    view.unmount();
  });

  it("shows lifetime usage without a plan or workspace gate, preserving the last value on a temporary error", async () => {
    vi.useFakeTimers();
    let unavailable = false;
    ipc.api("GET", /^\/usage\/totals$/, () => { if (unavailable) throw new Error("offline"); return totals(null, 0, 3600); });
    const view = render(<AccountUsage />);
    await tick();
    const usage = screen.getByRole("group", { name: "Lifetime account usage" });
    expect(usage).toHaveTextContent("60.0 total minutes used");
    expect(ipc.apiCalls()[0].path).toBe("/usage/totals");
    unavailable = true;
    await tick(30_000);
    expect(usage).toHaveTextContent("60.0 total minutes used");
    expect(usage).toHaveTextContent("Waiting for an update");
    expect(usage).not.toHaveTextContent(/price|cost|charge|subscription/);
    view.unmount();
  });

  it("does not count a guest's time as the host's consumed minutes", async () => {
    vi.useFakeTimers();
    const guest: SessionView = { ...live(), seat: { sessionId: "s1", seatId: "seat", hostName: "Host", mode: "meeting", title: null, capabilities: ["view"], hostConnected: true, sharing: true } };
    const view = render(<LiveUsage session={guest} />);
    await tick(30_000);
    expect(ipc.apiCalls()).toEqual([]);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    view.unmount();
  });
});

describe("live connected-time timer", () => {
  it("aligns the first tick after a delayed initial snapshot", async () => {
    vi.useFakeTimers();
    let finish!: (value: UsageTotals) => void;
    ipc.api("GET", /^\/usage\/totals$/, () => new Promise<UsageTotals>(resolve => { finish = resolve; }));
    const view = render(<LiveUsage session={live()} compact />);
    await tick(350);
    await act(async () => { finish(totals("s1", 60)); });
    expect(screen.getByRole("timer")).toHaveTextContent("01:00");
    await tick(1_000);
    expect(screen.getByRole("timer")).toHaveTextContent("01:01");
    view.unmount();
  });

  it("ticks every second, preserves progress through lagging checkpoints, and pauses during reconnect", async () => {
    vi.useFakeTimers();
    let recorded = totals("s1", 59);
    ipc.api("GET", /^\/usage\/totals$/, () => ({ ...recorded }));
    const view = render(<LiveUsage session={live()} compact />);
    await tick();
    const timer = screen.getByRole("timer", { name: "Call duration" });
    expect(timer).toHaveTextContent("00:59");
    await tick(1_000);
    expect(timer).toHaveTextContent("01:00");
    await tick(10_000);
    expect(timer).toHaveTextContent("01:10");
    // A new server snapshot behind the live estimate must not rewind it.
    recorded = totals("s1", 65);
    const reconnecting: SessionView = { ...live(), connection: { state: "reconnecting", attempt: 1, in_ms: 1000, close_code: null } };
    view.rerender(<LiveUsage session={reconnecting} compact />);
    await tick(60_000);
    expect(timer).toHaveTextContent("01:10");
    expect(timer).toHaveAttribute("title", expect.stringContaining("paused"));
    view.rerender(<LiveUsage session={live()} compact />);
    await tick(2_000);
    expect(timer).toHaveTextContent("01:12");
    expect(timer).toHaveAttribute("aria-live", "off");
    expect(ipc.apiCalls().length).toBeLessThan(15);
    view.unmount();
  });

  it("uses elapsed time when hidden, then reconciles the final recorded duration without ticking after end", async () => {
    vi.useFakeTimers();
    let recorded = totals("s1", 90);
    ipc.api("GET", /^\/usage\/totals$/, () => ({ ...recorded }));
    const view = render(<DataActivity.Provider value={true}><LiveUsage session={live()} compact /></DataActivity.Provider>);
    await tick();
    await tick(2_000);
    const timer = screen.getByRole("timer");
    expect(timer).toHaveTextContent("01:32");
    view.rerender(<DataActivity.Provider value={false}><LiveUsage session={live()} compact /></DataActivity.Provider>);
    const requests = ipc.apiCalls().length;
    await tick(30_000);
    expect(ipc.apiCalls()).toHaveLength(requests);
    expect(timer).toHaveTextContent("01:32");
    view.rerender(<DataActivity.Provider value={true}><LiveUsage session={live()} compact /></DataActivity.Provider>);
    expect(timer).toHaveTextContent("02:02");
    recorded = totals("s1", 121);
    const ended: SessionView = { ...live(), running: false, connection: { state: "stopped", reason: "ended" } };
    view.rerender(<DataActivity.Provider value={true}><LiveUsage session={ended} compact /></DataActivity.Provider>);
    await tick();
    expect(timer).toHaveTextContent("02:01");
    await tick(5_000);
    expect(timer).toHaveTextContent("02:01");
    view.unmount();
  });

  it("starts a new call from zero while awaiting its own snapshot and recovers from usage errors", async () => {
    vi.useFakeTimers();
    let fail = false;
    let finish!: (value: UsageTotals) => void;
    ipc.api("GET", /^\/usage\/totals$/, path => {
      if (path.includes("session_id=s2")) return new Promise<UsageTotals>(resolve => { finish = resolve; });
      if (fail) throw new Error("Usage unavailable");
      return totals("s1", 80);
    });
    const view = render(<LiveUsage session={live()} compact />);
    await tick();
    fail = true;
    await tick(11_000);
    expect(screen.getByRole("timer")).toHaveTextContent("01:31");
    expect(screen.getByRole("timer")).toHaveAttribute("title", expect.stringContaining("Waiting for a usage update"));
    view.rerender(<LiveUsage session={live("s2")} compact />);
    await tick(1_000);
    expect(screen.getByRole("timer")).toHaveTextContent("00:01");
    await act(async () => { finish(totals("s2", 5)); });
    expect(screen.getByRole("timer")).toHaveTextContent("00:05");
    view.unmount();
    const requests = ipc.apiCalls().length;
    await tick(60_000);
    expect(ipc.apiCalls()).toHaveLength(requests);
  });

  it("formats minute and hour boundaries without rounding up", () => {
    expect([0, 59.9, 60, 3599, 3600, 3661, 36000].map(connectedDuration)).toEqual(["00:00", "00:59", "01:00", "59:59", "1:00:00", "1:01:01", "10:00:00"]);
  });
});
