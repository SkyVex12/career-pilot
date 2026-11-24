import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UsageTotals } from "../src/api";
import { AccountUsage, LiveUsage } from "../src/Usage";
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
    expect(screen.getByLabelText("This call's consumed minutes")).toHaveTextContent("1.0 min used");
    recorded = totals("s1", 120, 660);
    const ended: SessionView = { ...live(), running: false, connection: { state: "stopped", reason: "ended" }, stopped: "ended" };
    view.rerender(<DataActivity.Provider value={true}><LiveUsage session={ended} compact /></DataActivity.Provider>);
    await tick();
    expect(screen.getByLabelText("This call's consumed minutes")).toHaveTextContent("2.0 min used");
    const before = ipc.apiCalls().length;
    view.rerender(<DataActivity.Provider value={false}><LiveUsage session={ended} compact /></DataActivity.Provider>);
    await tick(90_000);
    expect(ipc.apiCalls()).toHaveLength(before);
    expect(screen.getByLabelText("This call's consumed minutes")).toHaveTextContent("2.0 min used");
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
