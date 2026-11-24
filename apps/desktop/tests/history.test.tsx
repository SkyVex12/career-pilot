// History, the reference screen: its list states, filters, a call's detail, and deleting one.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SessionDetail as Detail, SessionItem } from "../src/api";
import { History, minutes, sessionTitle, statusBadge } from "../src/History";
import { SessionDetail } from "../src/SessionDetail";
import { ToastProvider } from "../src/ui";
import { ipc, pending } from "./ipc";

const iso = "2026-10-29T19:29:00Z";
const call = (id: string, mode: SessionItem["mode"], title: string | null, status: SessionItem["status"] = "ready", employer: string | null = null): SessionItem => ({
  id,
  mode,
  title,
  platform: "zoom",
  job_id: null,
  interview_id: null,
  employer_id: employer,
  store_transcript: true,
  started_at: iso,
  ended_at: status === "live" ? null : iso,
  billed_seconds: 52 * 60,
  me_audio_status: "live",
  status,
});

function routes(sessions: SessionItem[], next: string | null = null) {
  ipc.api("GET", /^\/sessions$/, (path) => (path.includes("before=") ? { sessions: [call("old", "meeting", "Older one")], next_before: null } : { sessions, next_before: next }));
  ipc.api("GET", /^\/usage$/, () => ({ period: "October", live_seconds: 9000, live_minutes: 150, sessions: sessions.length, measured_seconds: 9000, measured_sessions: sessions.length, legacy_estimated_seconds: 0, tracking_only: true, estimated_costs: [{ currency: "USD", estimated_minor: 1250, adjustment_minor: 0 }] }));
  ipc.api("GET", /^\/usage\/totals$/, () => ({ tracking_only: true, measurement: "connected-v1", as_of: iso, total_seconds: 18000, total_minutes: 300, total_sessions: 10, session_id: null, session_seconds: null, session_minutes: null, last_connected_at: iso }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [{ id: "e1", name: "Fernhill Logistics" }] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
}

const view = (props: Partial<Parameters<typeof History>[0]> = {}) =>
  render(
    <ToastProvider>
      <History running={false} liveId={null} onOpenLive={async () => {}} {...props} />
    </ToastProvider>,
  );

describe("History list", () => {
  it("loading: a skeleton in the list's place", () => {
    ipc.api("GET", /.*/, () => pending());
    view();
    expect(screen.getByRole("status")).toHaveTextContent("Loading calls");
  });

  it("error: what failed and a Retry that loads again", async () => {
    let fail = true;
    routes([call("s1", "interview", "Northwind technical")]);
    ipc.api("GET", /^\/sessions$/, () => {
      if (fail) throw "The API did not answer (HTTP 502)";
      return { sessions: [call("s1", "interview", "Northwind technical")], next_before: null };
    });
    view();
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 502");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /Interview · Northwind technical/ })).toBeInTheDocument();
  });

  it("empty: says what will appear", async () => {
    routes([]);
    view();
    expect(await screen.findByRole("heading", { name: "No calls yet" })).toBeInTheDocument();
  });

  it("signed out: asks to sign in, calls nothing", async () => {
    let asked = false;
    view({ ready: false, onSignIn: () => (asked = true) });
    await userEvent.click(screen.getByRole("button", { name: "Open settings" }));
    expect(asked).toBe(true);
    expect(ipc.apiCalls()).toHaveLength(0);
  });

  it("loaded: rows in words, filters, older calls", async () => {
    routes([call("s1", "interview", "Northwind technical"), call("s2", "standup", null, "processing", "e1")], "cursor");
    view();
    const list = await screen.findByRole("list", { name: "Past calls" });
    expect(within(list).getByText("Interview · Northwind technical")).toBeInTheDocument();
    expect(within(list).getByText("Standup · Fernhill Logistics")).toBeInTheDocument();
    expect(within(list).getByText("Processing")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Account usage" })).toHaveTextContent(/150 connected minutes in October \(UTC\) · 2 calls/);
    await waitFor(() => expect(screen.getByRole("group", { name: "Lifetime account usage" })).toHaveTextContent("300.0 total minutes used"));
    expect(screen.queryByText(/estimated cost/)).not.toBeInTheDocument();

    expect(screen.queryByLabelText("Standups")).toBeNull();
    expect(screen.queryByLabelText("Meetings")).toBeNull();
    await userEvent.click(screen.getByLabelText("Work"));
    expect(screen.queryByText("Interview · Northwind technical")).not.toBeInTheDocument();
    expect(screen.getByText("Standup · Fernhill Logistics")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    expect(await screen.findByText("Meeting · Older one")).toBeInTheDocument();
    expect(screen.getByText("Standup · Fernhill Logistics")).toBeVisible();
    await userEvent.click(screen.getByLabelText("Interviews"));
    expect(screen.getByText("Interview · Northwind technical")).toBeVisible();
    expect(screen.queryByText("Standup · Fernhill Logistics")).toBeNull();
    expect(screen.queryByText("Meeting · Older one")).toBeNull();
  });

  it("opening a call shows its detail, and Back returns", async () => {
    routes([call("s1", "interview", "Northwind technical")]);
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ ...call("s1", "interview", "Northwind technical"), questions: 6, outputs: { summary: "It went well." } }) satisfies Detail);
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({ session_id: "s1", stored: true, started_at: iso, segments: [], questions: [] }));
    view();
    await userEvent.click(await screen.findByRole("button", { name: /Northwind technical/ }));
    expect(await screen.findByRole("heading", { name: "Interview · Northwind technical" })).toBeInTheDocument();
    expect(await screen.findByText("It went well.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "← Past calls" }));
    expect(await screen.findByRole("list", { name: "Past calls" })).toBeInTheDocument();
  });

  it("keeps historical estimates separate from consumed minutes and always refreshes usage", async () => {
    routes([]);
    ipc.api("GET", /^\/usage$/, () => ({ period: "2026-11", live_seconds: 4200, live_minutes: 70, sessions: 3, measured_seconds: 600, measured_sessions: 1, legacy_estimated_seconds: 3600, tracking_only: true, estimated_costs: [{ currency: "USD", estimated_minor: 58, adjustment_minor: -25 }] }));
    view();
    const summary = await screen.findByRole("group", { name: "Account usage" });
    expect(summary).toHaveTextContent("10 connected minutes in 2026-11 (UTC) · 1 call");
    expect(summary).toHaveTextContent("Historical estimate: 60 minutes · separate from recorded usage.");
    expect(summary).not.toHaveTextContent(/cost|charges|adjustments/);
    const usageReads = ipc.calls.filter(call => call.cmd === "api_call" && call.args.path === "/usage");
    expect(usageReads.length).toBeGreaterThan(0);
    expect(usageReads.every(call => call.args.refresh === true)).toBe(true);
  });

  it("labels an older server's totals as historical instead of measured usage", async () => {
    routes([]);
    ipc.api("GET", /^\/usage$/, () => ({ period: "2026-11", live_seconds: 300, live_minutes: 5, sessions: 1 }));
    view();
    const summary = await screen.findByRole("group", { name: "Account usage" });
    expect(summary).toHaveTextContent("0 connected minutes");
    expect(summary).toHaveTextContent("Historical estimate: 5 minutes · separate from recorded usage.");
  });

  it("settles final usage after a local call ends and keeps remote usage fresh only while visible", async () => {
    vi.useFakeTimers();
    let mounted: ReturnType<typeof view> | undefined;
    try {
      routes([]);
      let measuredSeconds = 60;
      ipc.api("GET", /^\/usage$/, () => ({
        period: "2026-11", live_seconds: measuredSeconds, live_minutes: Math.ceil(measuredSeconds / 60),
        sessions: 1, measured_seconds: measuredSeconds, measured_sessions: 1,
        legacy_estimated_seconds: 0, tracking_only: true,
        estimated_costs: [{ currency: "USD", estimated_minor: Math.round(measuredSeconds * 500 / 3600), adjustment_minor: 0 }],
      }));
      await act(async () => { mounted = view({ running: true, liveId: "local-call" }); });
      const summary = screen.getByRole("group", { name: "Account usage" });
      expect(summary).toHaveTextContent("1 connected minutes");
      measuredSeconds = 90;
      await act(async () => vi.advanceTimersByTimeAsync(15_000));
      expect(summary).toHaveTextContent("1.5 connected minutes");

      mounted!.rerender(<ToastProvider><History running={false} liveId={null} onOpenLive={async () => {}} /></ToastProvider>);
      // The final worker checkpoint becomes durable after the UI has stopped its call.
      measuredSeconds = 120;
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(summary).toHaveTextContent("2 connected minutes");

      // Another device continues using the account while this desktop stays idle.
      measuredSeconds = 180;
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(summary).toHaveTextContent("3 connected minutes");
      const reads = () => ipc.calls.filter(call => call.cmd === "api_call" && call.args.path === "/usage");
      expect(reads().slice(1).every(call => call.args.wake === false && call.args.refresh === true)).toBe(true);
      const beforeHide = reads().length;
      mounted!.rerender(<ToastProvider><History active={false} running={false} liveId={null} onOpenLive={async () => {}} /></ToastProvider>);
      await act(async () => vi.advanceTimersByTimeAsync(90_000));
      expect(reads()).toHaveLength(beforeHide);
    } finally {
      mounted?.unmount();
      vi.useRealTimers();
    }
  });

  it("uses opaque older-page cursors and preserves loaded pages during an explicit refresh", async () => {
    routes([]);
    ipc.api("GET", /^\/sessions$/, path => path.includes("cursor=opaque") ? {
      sessions: [call("old", "meeting", "Older call")], next_before: null, next_cursor: null,
    } : { sessions: [call("new", "meeting", "Newest call")], next_before: null, next_cursor: "opaque" });
    view();
    await screen.findByText("Meeting · Newest call");
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    await screen.findByText("Meeting · Older call");
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).not.toBeDisabled());
    expect(screen.getByText("Meeting · Older call")).toBeVisible();
    const reads = ipc.calls.filter(call => call.cmd === "api_call" && String(call.args.path).startsWith("/sessions?"));
    expect(reads.map(call => call.args.path)).toEqual(["/sessions?limit=20", "/sessions?limit=20&cursor=opaque", "/sessions?limit=20", "/sessions?limit=20&cursor=opaque"]);
    expect(reads.slice(-2).every(call => call.args.refresh === true)).toBe(true);
  });

  it("searches loaded calls and keeps older calls reachable with a filter", async () => {
    routes([call("s1", "interview", "Northwind technical"), call("s2", "standup", null, "ready", "e1")], "cursor");
    view();
    await screen.findByRole("list", { name: "Past calls" });
    await userEvent.type(screen.getByRole("searchbox", { name: "Search past calls" }), "Fernhill");
    expect(screen.queryByText("Interview · Northwind technical")).not.toBeInTheDocument();
    expect(screen.getByText("Standup · Fernhill Logistics")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Interviews"));
    expect(screen.getByRole("heading", { name: "No matching calls" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load older calls" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    await userEvent.click(screen.getByLabelText("Work"));
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    expect(await screen.findByText("Meeting · Older one")).toBeInTheDocument();
  });
});

describe("A call's detail", () => {
  const ready = (over: Partial<Detail> = {}): Detail => ({
    ...call("s1", "interview", "Northwind technical"),
    questions: 6,
    notes: "",
    outputs: { summary: "Summary text.", follow_up_email: "Hi Jordan," },
    ...over,
  });

  it("waiting: says what it waits for", () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => pending());
    render(<SessionDetail sessionId="s1" />);
    expect(screen.getByRole("status")).toHaveTextContent("Ending the session…");
  });

  it("ready: outputs, then Replay and Transcript tabs", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready());
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({
      session_id: "s1",
      stored: true,
      started_at: iso,
      segments: [{ channel: "them", seg_id: 1, start_ms: 2000, end_ms: 5000, text: "Tell me about an outage." }],
      questions: [],
    }));
    render(
      <ToastProvider>
        <SessionDetail sessionId="s1" />
      </ToastProvider>,
    );
    expect(await screen.findByText("Summary text.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.queryByText("Hi Jordan,")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Follow-up" }));
    expect(screen.getByText("Hi Jordan,")).toBeInTheDocument();
    expect(screen.queryByText("Summary text.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    expect(await screen.findByText("Tell me about an outage.")).toBeInTheDocument();
  });

  it("copies the complete transcript in time order without per-entry actions", async () => {
    const user = userEvent.setup();
    ipc.api("GET", /^\/sessions\/s1$/, () => ready());
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({
      session_id: "s1", stored: true, started_at: iso, questions: [],
      segments: [
        { channel: "me", seg_id: 2, start_ms: 62000, end_ms: 65000, text: "First line.\nSecond line." },
        { channel: "them", seg_id: 1, start_ms: 59000, end_ms: 61000, text: "How did you approach it?" },
      ],
    }));
    render(<ToastProvider><SessionDetail sessionId="s1" /></ToastProvider>);
    await user.click(await screen.findByRole("tab", { name: "Transcript" }));
    await screen.findByText("How did you approach it?");
    const panel = screen.getByRole("tabpanel", { name: "Transcript" });
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    await user.click(within(panel).getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe("0:59 Them: How did you approach it?\n1:02 You: First line.\nSecond line.");
    expect(await screen.findByText("Copied")).toBeVisible();
  });

  it("does not offer transcript copying while loading or when no speech was stored", async () => {
    const user = userEvent.setup();
    let finish!: (value: unknown) => void;
    ipc.api("GET", /^\/sessions\/s1$/, () => ready());
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => new Promise(resolve => { finish = resolve; }));
    render(<SessionDetail sessionId="s1" />);
    await user.click(await screen.findByRole("tab", { name: "Transcript" }));
    const panel = screen.getByRole("tabpanel", { name: "Transcript" });
    expect(within(panel).getByRole("status")).toHaveTextContent("Loading the transcript");
    expect(within(panel).queryByRole("button", { name: "Copy" })).toBeNull();
    await act(async () => finish({ session_id: "s1", stored: true, started_at: iso, questions: [], segments: [] }));
    expect(await within(panel).findByText("Nothing was transcribed")).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it.each(["success", "refused", "throws"])("handles clipboard fallback %s without leaving an editor or losing focus", async result => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Unavailable"));
    const previous = Object.getOwnPropertyDescriptor(document, "execCommand");
    const fallback = vi.fn(() => {
      expect(document.querySelector("textarea")?.value).toBe("Summary text.");
      if (result === "throws") throw new Error("Blocked");
      return result === "success";
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: fallback });
    try {
      ipc.api("GET", /^\/sessions\/s1$/, () => ready());
      render(<ToastProvider><SessionDetail sessionId="s1" /></ToastProvider>);
      const copy = await screen.findByRole("button", { name: "Copy" });
      await user.click(copy);
      expect(fallback).toHaveBeenCalledWith("copy");
      expect(document.querySelector("textarea")).toBeNull();
      expect(copy).toHaveFocus();
      if (result === "success") expect(await screen.findByText("Copied")).toBeVisible();
      else {
        expect(await screen.findByText("Could not copy. Select the text and copy it manually.")).toBeVisible();
        expect(screen.queryByText("Copied")).toBeNull();
      }
    } finally {
      write.mockRestore();
      if (previous) Object.defineProperty(document, "execCommand", previous);
      else Reflect.deleteProperty(document, "execCommand");
    }
  });
  it("no transcript stored: outputs stay available without replay requests", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false }));
    render(<SessionDetail sessionId="s1" />);
    expect(await screen.findByText(/transcript was not stored/)).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Call review" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Transcript" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Replay" })).not.toBeInTheDocument();
    expect(ipc.apiCalls().some((c) => c.path.endsWith("/replay"))).toBe(false);
  });

  it("notes survive tab changes, save, reload, and discard without storing a transcript", async () => {
    let saved = "Original note";
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false, notes: saved }));
    ipc.api("PUT", /^\/sessions\/s1\/notes$/, (_path, body) => ({ notes: saved = (body as { text: string }).text }));
    const view = render(<SessionDetail sessionId="s1" />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue("Original note");
    await userEvent.type(screen.getByLabelText("Call notes"), " + follow up");
    await userEvent.click(screen.getByRole("tab", { name: "Summary" }));
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue("Original note + follow up");
    await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save notes" })).toBeDisabled());
    expect(ipc.apiCalls("PUT")[0]).toMatchObject({ path: "/sessions/s1/notes", body: { text: "Original note + follow up" } });
    await userEvent.type(screen.getByLabelText("Call notes"), " discard this");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue(saved);
    view.unmount();
    render(<SessionDetail sessionId="s1" />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue(saved);
  });

  it("failed note saves preserve the draft and Retry saves it", async () => {
    let fail = true;
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false, notes: "" }));
    ipc.api("PUT", /^\/sessions\/s1\/notes$/, (_path, body) => {
      if (fail) throw "Connection lost";
      return { notes: (body as { text: string }).text };
    });
    render(<SessionDetail sessionId="s1" />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    await userEvent.type(screen.getByLabelText("Call notes"), "Keep this draft");
    await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.getByLabelText("Call notes")).toHaveValue("Keep this draft");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Save notes" })).toBeDisabled();
  });

  it("standup report opens first and feedback has its own tab", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ mode: "standup", store_transcript: false, outputs: { summary: "Call summary", standup_report: "Yesterday: finished migration.", feedback: "Keep the update short." } }));
    render(<SessionDetail sessionId="s1" />);
    expect(await screen.findByText("Yesterday: finished migration.")).toBeInTheDocument();
    expect(screen.queryByText("Keep the update short.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Feedback" }));
    expect(screen.getByText("Keep the update short.")).toBeInTheDocument();
  });

  it("returning to past calls confirms before discarding unsaved notes", async () => {
    let closed = false;
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false, notes: "" }));
    render(<SessionDetail sessionId="s1" onClose={() => { closed = true; }} />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    await userEvent.type(screen.getByLabelText("Call notes"), "Unsaved follow-up");
    await userEvent.click(screen.getByRole("button", { name: "← Past calls" }));
    expect(closed).toBe(false);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue("Unsaved follow-up");
    await userEvent.click(screen.getByRole("button", { name: "← Past calls" }));
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard notes" }));
    await waitFor(() => expect(closed).toBe(true));
  });

  it("switching sessions resets the Notes draft to that call's saved notes", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false, notes: "First call" }));
    ipc.api("GET", /^\/sessions\/s2$/, () => ready({ id: "s2", store_transcript: false, notes: "Second call", outputs: { summary: "Second summary" } }));
    const view = render(<SessionDetail sessionId="s1" />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    await userEvent.type(screen.getByLabelText("Call notes"), " extra");
    view.rerender(<SessionDetail sessionId="s2" />);
    await screen.findByText("Second summary");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    expect(screen.getByLabelText("Call notes")).toHaveValue("Second call");
  });

  it("delete asks first, then deletes", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false }));
    ipc.api("DELETE", /^\/sessions\/s1$/, () => null);
    let deleted = false;
    render(<SessionDetail sessionId="s1" onDeleted={() => (deleted = true)} />);
    await screen.findByText("Summary text.");
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete call…" }));
    expect(ipc.apiCalls("DELETE")).toHaveLength(0);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete call" }));
    await waitFor(() => expect(deleted).toBe(true));
    expect(ipc.apiCalls("DELETE")[0].path).toBe("/sessions/s1");
  });
});

describe("History helpers", () => {
  it("titles, minutes and status in words", () => {
    expect(sessionTitle(call("a", "standup", null, "ready", "e1"), { e1: "Fernhill" })).toBe("Standup · Fernhill");
    expect(sessionTitle(call("a", "interview", null), {})).toBe("Interview");
    expect(minutes(call("a", "interview", null))).toBe("52 min");
    expect(minutes(call("a", "interview", null, "live"))).toBe("");
    expect(statusBadge("processing")).toEqual({ tone: "warning", text: "Processing" });
  });
});

describe("History accessibility", () => {
  it("has no axe violations, loaded and in a call's detail", async () => {
    const axe = (await import("axe-core")).default;
    const check = async () => {
      const r = await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
      return r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
    };
    routes([call("s1", "interview", "Northwind technical"), call("s2", "standup", null, "processing", "e1")], "cursor");
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ ...call("s1", "interview", "Northwind technical"), questions: 6, outputs: { summary: "It went well.", follow_up_email: "Hi" } }) satisfies Detail);
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({ session_id: "s1", stored: true, started_at: iso, segments: [], questions: [] }));
    view();
    await screen.findByRole("list", { name: "Past calls" });
    expect(await check()).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: /Northwind technical/ }));
    await screen.findByText("It went well.");
    expect(await check()).toEqual([]);
  });
});

  it("shows an honest empty state for old metadata-only summaries, without a fake Copy action", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ ...call("s1", "interview", "Audio test"), questions: 0, notes: "", outputs: { summary: "Interview of about 1 minutes. No questions detected.\n\nThe transcript was not stored for this session, so there is no written summary." } }));
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({ session_id: "s1", stored: true, started_at: iso, segments: [], questions: [] }));
    render(<SessionDetail sessionId="s1" />);
    expect(await screen.findByRole("heading", { name: "No conversation was captured" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add call notes" }));
    expect(await screen.findByLabelText("Call notes")).toBeInTheDocument();
  });
