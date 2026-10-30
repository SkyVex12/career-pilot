// History, the reference screen: its list states, filters, a call's detail, and deleting one.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
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
  ipc.api("GET", /^\/usage$/, () => ({ period: "October", live_seconds: 9000, live_minutes: 150, sessions: sessions.length }));
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
    expect(screen.getByText(/150 live minutes in October · 2 calls/)).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Standups"));
    expect(screen.queryByText("Interview · Northwind technical")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Meetings"));
    expect(screen.getByRole("heading", { name: "No meetings yet" })).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("All"));

    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    expect(await screen.findByText("Meeting · Older one")).toBeInTheDocument();
  });

  it("opening a call shows its detail, and Back returns", async () => {
    routes([call("s1", "interview", "Northwind technical")]);
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ ...call("s1", "interview", "Northwind technical"), questions: 6, outputs: { summary: "It went well." } }) satisfies Detail);
    ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({ session_id: "s1", stored: true, started_at: iso, segments: [], questions: [] }));
    view();
    await userEvent.click(await screen.findByRole("button", { name: /Northwind technical/ }));
    expect(await screen.findByRole("heading", { name: "Interview · Northwind technical" })).toBeInTheDocument();
    expect(await screen.findByText("It went well.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "← History" }));
    expect(await screen.findByRole("list", { name: "Past calls" })).toBeInTheDocument();
  });
});

describe("A call's detail", () => {
  const ready = (over: Partial<Detail> = {}): Detail => ({
    ...call("s1", "interview", "Northwind technical"),
    questions: 6,
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
    await userEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    expect(await screen.findByText("Tell me about an outage.")).toBeInTheDocument();
  });

  it("no transcript stored: no tabs, and it says so", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ready({ store_transcript: false }));
    render(<SessionDetail sessionId="s1" />);
    expect(await screen.findByText(/transcript was not stored/)).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
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
