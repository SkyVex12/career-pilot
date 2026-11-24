import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CallScreen, type CallProps } from "../src/CallScreen";
import { GuestInvites } from "../src/seats";
import { guestCapabilities, guestScope, invitePlannedGuests, inviteProgress, recentGuests, rememberGuest } from "../src/guestStorage";
import { initial, reduce } from "../src/session";
import { ToastProvider } from "../src/ui";
import { app, detail, employer, interview, resume, SETTINGS, settingsView } from "./fixtures";
import { ipc } from "./ipc";

const role = detail(app("a1", "Northwind", "Backend"));
const scope = guestScope(SETTINGS, "alex@example.com");
const props = (over: Partial<CallProps> = {}): CallProps => ({ session: initial, tokenSet: true, environment: "cloud", release: true, signedInAs: "alex@example.com", settings: SETTINGS, careerOs: { connected: true, available: true }, locked: false, onCareerOsChanged() {}, onSettingsSaved() {}, onSettings() {}, onResumes() {}, onOpenLive: async () => {}, ...over });
function routes() {
  ipc.handle("get_settings", () => settingsView());
  ipc.handle("live_status", () => ({ running: false, session_id: null, levels: null, stats: null }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
  ipc.api("GET", /^\/applications$/, () => ({ applications: [role] }));
  ipc.api("GET", /^\/applications\/a1$/, () => role);
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [interview("i1", "a1", "Northwind", 60)] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Base"), versions: [] }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/seats$/, () => ({ seats: [] }));
  ipc.api("GET", /^\/sessions\/s1\/seats$/, () => ({ seats: [], voice: true, screen: true }));
}
const tree = (over: Partial<CallProps> = {}) => <ToastProvider><CallScreen {...props(over)} /></ToastProvider>;
async function chooseGuest(email: string) {
  if (!screen.getByText("Customize").closest("details")?.open) await userEvent.click(screen.getByText("Customize"));
  await userEvent.click(screen.getByRole("button", { name: /Add assistants|Change assistants/ }));
  const picker = screen.getByRole("dialog", { name: "Add assistants" });
  await userEvent.type(within(picker).getByRole("textbox", { name: "Find assistant by name or email" }), email);
  await userEvent.click(within(picker).getByRole("checkbox", { name: new RegExp(email) }));
  return picker;
}

describe("the Call setup design", () => {
  it("uses calendar context when returning to Call", async () => {
    routes();
    render(tree({ preset: { target: "job:a1", meetingTitle: "Team interview", mode: "interview", n: 1 } }));
    await screen.findByText("We build routing.");
    expect(screen.queryByText("Meeting preparation")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args.request).toMatchObject({ job_id: "a1", meeting_title: "Team interview" });
  });

  it("replaces the audio summaries with inline pickers and tests", async () => {
    routes();
    render(tree());
    await screen.findByText("We build routing.");
    await userEvent.click(screen.getByRole("button", { name: "Change or test" }));
    expect(screen.getByRole("combobox", { name: "Microphone" })).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Test" })).toHaveLength(2);
    expect(document.querySelector(".audio-pickers")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("combobox", { name: "Microphone" })).not.toBeInTheDocument();
  });

  it.each([false, true])("uses the application's attached resume without changing its pairing (manual: %s)", async manual => {
    routes();
    let current = { ...role, resume: { version_id: "rt1", kind: "tailored" as const, label: "Backend version", status: "extracted" as const, manual } };
    ipc.api("GET", /^\/applications\/a1$/, () => current);
    ipc.api("PATCH", /^\/applications\/a1$/, (_path, body) => { current = { ...current, ...(body as object) }; return current; });
    const onApplications = vi.fn();
    render(tree({ onApplications }));
    expect(await screen.findByText("Resume · Backend version · Ready")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Round" })).not.toBeInTheDocument();
    expect(screen.queryByText("Upcoming")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByRole("dialog", { name: "Add application" })).toBeVisible();
    expect(onApplications).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(ipc.apiCalls("PATCH")).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = screen.getByRole("textbox", { name: "Job description" });
    await userEvent.clear(editor); await userEvent.type(editor, "Own reliable platform services.");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Own reliable platform services.")).toBeVisible();
    expect(ipc.apiCalls("PATCH").map(call => call.body)).toEqual([{ jd_text: "Own reliable platform services." }]);
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1");
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args.request).toMatchObject({ job_id: "a1", interview_id: null });
  });

  it("preserves an explicit interview started from Interview prep without a round selector", async () => {
    routes();
    render(tree({ preset: { target: "interview:i1", n: 1 } }));
    await screen.findByText("We build routing.");
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1");
    expect(screen.queryByRole("combobox", { name: "Round" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args.request).toMatchObject({ job_id: "a1", interview_id: "i1" });
  });

  it("uses the newly selected application's resume and clears an explicit interview target", async () => {
    routes();
    const other = { ...detail(app("a2", "Lumen", "Design")), resume: { version_id: "rt2", kind: "tailored" as const, label: "Lumen design", status: "extracting" as const, manual: true } };
    ipc.api("GET", /^\/applications$/, () => ({ applications: [role, other] }));
    ipc.api("GET", /^\/applications\/a2$/, () => other);
    render(tree({ preset: { target: "interview:i1", n: 1 } }));
    await screen.findByText("We build routing.");
    await userEvent.click(screen.getByRole("combobox", { name: "Application" }));
    await userEvent.click(screen.getByRole("option", { name: "Lumen · Design" }));
    expect(await screen.findByText("Resume · Lumen design · Reading")).toBeVisible();
    expect(screen.queryByText("Resume · Base · Ready")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args.request).toMatchObject({ job_id: "a2", interview_id: null });
  });

  it("offers the base resume editor when a general interview has no base resume", async () => {
    routes();
    ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [resume("rt1", "tailored", "Role-specific")] }));
    const onResumes = vi.fn();
    render(tree({ onResumes }));
    await screen.findByText("We build routing.");
    await userEvent.click(screen.getByRole("combobox", { name: "Application" }));
    await userEvent.click(screen.getByRole("option", { name: "General interview · Base resume" }));
    expect(await screen.findByText("No resume attached")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Add resume" }));
    expect(onResumes).toHaveBeenCalledOnce();
    expect(screen.queryByText("Role-specific")).not.toBeInTheDocument();
  });

  it("keeps an explicit base-resume choice after returning from another tab", async () => {
    routes();
    const view = render(tree());
    await screen.findByText("We build routing.");
    await userEvent.click(screen.getByRole("combobox", { name: "Application" }));
    await userEvent.click(screen.getByRole("option", { name: "General interview · Base resume" }));
    view.rerender(tree({ active: false }));
    view.rerender(tree({ active: true }));
    await waitFor(() => expect(ipc.apiCalls("GET").filter(call => call.path === "/applications").length).toBeGreaterThan(1));
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args.request).toMatchObject({ job_id: null, interview_id: null });
  });
  it("sends selected guests once only after Start succeeds and the new host session exists", async () => {
    routes();
    let finish!: () => void;
    ipc.handle("start_session", () => new Promise<void>(resolve => { finish = resolve; }));
    ipc.api("POST", /^\/sessions\/s1\/seats$/, () => ({ status: "sent" }));
    const view = render(tree());
    await screen.findByText("We build routing.");
    const picker = await chooseGuest("dana@example.com");
    await userEvent.click(within(picker).getByRole("checkbox", { name: "Control my mouse and keyboard" }));
    expect(within(picker).getByRole("checkbox", { name: "View my shared screen" })).toBeChecked();
    await userEvent.keyboard("{Escape}");
    expect(recentGuests(scope)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.apiCalls("POST")).toEqual([]);
    view.rerender(tree({ session: { ...initial, running: true, sessionId: "s1", connection: { state: "live", session_id: "s1", resumed: false } } }));
    expect(ipc.apiCalls("POST")).toEqual([]);
    await act(async () => { finish(); });
    await waitFor(() => expect(ipc.apiCalls("POST")).toHaveLength(1));
    expect(ipc.apiCalls("POST")[0]).toEqual({ method: "POST", path: "/sessions/s1/seats", body: { email: "dana@example.com", capabilities: ["view", "note", "voice", "device.view", "device.control"] } });
    await waitFor(() => expect(recentGuests(scope)).toEqual([{ email: "dana@example.com" }]));
    view.rerender(tree({ session: { ...initial, running: true, sessionId: "s1" } }));
    expect(ipc.apiCalls("POST")).toHaveLength(1);
  });

  it("does not invite or remember contacts when Start fails", async () => {
    routes();
    ipc.handle("start_session", () => { throw "Microphone unavailable"; });
    render(tree());
    await screen.findByText("We build routing.");
    await chooseGuest("dana@example.com");
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(await screen.findByText("Microphone unavailable")).toBeVisible();
    expect(ipc.apiCalls("POST")).toEqual([]);
    expect(recentGuests(scope)).toEqual([]);
  });

  it("retains selected guests, caps the picker at two, and clears control when screen is removed", async () => {
    routes();
    rememberGuest(scope, { email: "dana@example.com", name: "Dana Singh" });
    rememberGuest(scope, { email: "lee@example.com", name: "Lee Park" });
    rememberGuest(scope, { email: "sam@example.com", name: "Sam Jones" });
    render(tree());
    await screen.findByText("We build routing.");
    await userEvent.click(screen.getByText("Customize"));
    await userEvent.click(screen.getByRole("button", { name: "Add assistants" }));
    const picker = screen.getByRole("dialog", { name: "Add assistants" });
    await userEvent.click(within(picker).getByRole("checkbox", { name: /Dana Singh/ }));
    await userEvent.click(within(picker).getByRole("checkbox", { name: /Control my mouse/ }));
    await userEvent.click(within(picker).getByRole("checkbox", { name: "View my shared screen" }));
    expect(within(picker).getByRole("checkbox", { name: "Control my mouse and keyboard" })).not.toBeChecked();
    await userEvent.click(within(picker).getByRole("checkbox", { name: /Lee Park/ }));
    expect(within(picker).getByRole("checkbox", { name: /Sam Jones/ })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Remove Dana Singh" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  });

  it("passes meeting context, focus and transcript preference to the real start command", async () => {
    routes();
    ipc.api("GET", /^\/employers$/, () => ({ employers: [employer("e1", "Fernhill")] }));
    ipc.api("POST", /^\/employers\/e1\/packs$/, () => ({ status: "ready" }));
    ipc.api("GET", /^\/employers\/e1\/documents$/, () => ({ documents: [{ id: "d1", name: "Architecture", status: "ingested", approved_proposals: 3 }] }));
    render(tree());
    await userEvent.click(screen.getByRole("radio", { name: "For work" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Agenda" }), "Discuss routing decisions");
    await userEvent.click(screen.getByText("Focus on documents"));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Architecture (3 facts)" }));
    await userEvent.click(screen.getByText("Customize"));
    await userEvent.click(screen.getByRole("checkbox", { name: "Save transcript" }));
    await userEvent.click(screen.getByRole("radio", { name: "Paragraphs" }));
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find(c => c.cmd === "start_session")?.args.request).toMatchObject({ mode: "meeting", employer_id: "e1", agenda: "Discuss routing decisions", material_ids: ["d1"], answer_style: "paragraph", store_transcript: false });
  });
});

describe("guest contact privacy and delivery", () => {
  it("isolates recents by account, profile and environment and only learns server names", async () => {
    routes();
    ipc.api("GET", /^\/sessions\/s1\/seats$/, () => ({ voice: false, screen: false, seats: [{ email: "dana@example.com", name: "Dana Singh" }] }));
    ipc.api("POST", /^\/sessions\/s1\/seats$/, () => ({ status: "sent" }));
    await invitePlannedGuests("s1", scope, [{ email: "DANA@example.com", screen: true, control: true }]);
    expect(recentGuests(scope)).toEqual([{ email: "dana@example.com", name: "Dana Singh" }]);
    expect(recentGuests(guestScope({ ...SETTINGS, profile: "work" }, "alex@example.com"))).toEqual([]);
    expect(recentGuests(guestScope(SETTINGS, "other@example.com"))).toEqual([]);
    expect(guestCapabilities({ email: "dana@example.com", screen: true, control: true }, { screen: false, voice: false })).toEqual(["note"]);
  });
  it("keeps failed automatic invitations available for retry without remembering unsent people", async () => {
    routes();
    ipc.api("POST", /^\/sessions\/s1\/seats$/, () => { throw "Network unavailable"; });
    const guest = { email: "dana@example.com", screen: false, control: false };
    await invitePlannedGuests("s1", scope, [guest]);
    expect(inviteProgress("s1")).toMatchObject({ pending: 0, failures: [{ guest, error: "Network unavailable" }] });
    expect(recentGuests(scope)).toEqual([]);
    expect(inviteProgress("another-session")).toBeNull();
  });
  it("releases the workspace busy lock when the guest view replaces Join during connection", async () => {
    ipc.api("GET", /^\/seats$/, () => ({ seats: [{ seat_id: "seat2", session_id: "session2", host_name: "Dana Singh", mode: "meeting", title: null, status: "admitted", capabilities: ["view"], expires_at: null }] }));
    let finish!: () => void;
    ipc.handle("seat_join", () => new Promise<void>(resolve => { finish = resolve; }));
    const busy = vi.fn();
    const view = render(<GuestInvites running={false} onBusyChange={busy} />);
    await userEvent.click(await screen.findByRole("button", { name: "Assist a call 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Assist" }));
    expect(busy).toHaveBeenLastCalledWith(true);
    view.unmount();
    expect(busy).toHaveBeenLastCalledWith(false);
    await act(async () => { finish(); });
  });
  it("reveals assistance invitations when polling finds one and hides the menu when it expires", async () => {
    vi.useFakeTimers();
    try {
      let seats: object[] = [];
      ipc.api("GET", /^\/seats$/, () => ({ seats: [...seats] }));
      const count = vi.fn();
      await act(async () => { render(<GuestInvites running={false} onCountChange={count} />); });
      expect(screen.queryByRole("button", { name: /Assist a call/ })).not.toBeInTheDocument();
      seats = [{ seat_id: "seat2", session_id: "session2", host_name: "Dana Singh", mode: "meeting", title: "Design review", status: "admitted", capabilities: ["view", "note"], expires_at: new Date(Date.now() + 6000).toISOString() }];
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      fireEvent.click(screen.getByRole("button", { name: "Assist a call 1" }));
      expect(screen.getByRole("dialog", { name: "Invitations to assist" })).toBeInTheDocument();
      expect(count).toHaveBeenLastCalledWith(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.queryByRole("button", { name: /Assist a call/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: "Invitations to assist" })).not.toBeInTheDocument();
      expect(count).toHaveBeenLastCalledWith(0);
    } finally { vi.useRealTimers(); }
  });
  it("hides the action without invitations and assists the exact invitation from its dropdown", async () => {
    let seats: object[] = [];
    ipc.api("GET", /^\/seats$/, () => ({ seats }));
    const view = render(<GuestInvites running={false} />);
    expect(screen.queryByRole("button", { name: /Assist a call/ })).not.toBeInTheDocument();
    seats = [{ seat_id: "seat2", session_id: "session2", host_name: "Dana Singh", mode: "meeting", title: "Design review", status: "admitted", capabilities: ["view", "note"], expires_at: null }];
    view.rerender(<GuestInvites running />); view.rerender(<GuestInvites running={false} />);
    await userEvent.click(await screen.findByRole("button", { name: "Assist a call 1" }));
    expect(screen.getByRole("dialog", { name: "Invitations to assist" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Invitations to assist" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Assist a call 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Assist" }));
    expect(ipc.calls.find(c => c.cmd === "seat_join")?.args).toEqual({ request: { session_id: "session2", seat_id: "seat2" } });
  });
});
describe("workspace startup after a call", () => {
  it("opens Start call when a previous ended session is restored, without loading its review", async () => {
    routes();
    render(tree({ session: { ...initial, sessionId: "s1", stopped: "ended" } }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New call" })).not.toBeInTheDocument();
    expect(ipc.apiCalls().filter(c => c.path === "/sessions/s1")).toHaveLength(0);
  });
  it("waits for the stopped event when the native connection ends in an earlier render", async () => {
    routes();
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ id: "s1", status: "ready", mode: "interview", store_transcript: false, questions: 1, outputs: { summary: "The split-event call is complete." }, notes: "" }));
    const live = reduce(initial, { type: "event", event: { kind: "connection", state: { state: "live", session_id: "s1", resumed: false } } });
    const disconnected = reduce(live, { type: "event", event: { kind: "connection", state: { state: "stopped", reason: "ended" } } });
    const view = render(tree({ session: live }));
    view.rerender(tree({ session: disconnected }));
    expect(disconnected.stopped).toBeNull();
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    expect(ipc.apiCalls().filter(c => c.path === "/sessions/s1")).toHaveLength(0);

    // The native engine emits this separately, after the connection event above.
    view.rerender(tree({ session: reduce(disconnected, { type: "event", event: { kind: "stopped", reason: "ended" } }) }));
    expect(await screen.findByText("The split-event call is complete.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New call" })).toBeInTheDocument();
  });
  it.each(["replaced", "no_minutes"] as const)("does not open review for separate %s stop events", async reason => {
    routes();
    const live = reduce(initial, { type: "event", event: { kind: "connection", state: { state: "live", session_id: "s1", resumed: false } } });
    const disconnected = reduce(live, { type: "event", event: { kind: "connection", state: { state: "stopped", reason } } });
    const view = render(tree({ session: live }));
    view.rerender(tree({ session: disconnected }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    view.rerender(tree({ session: reduce(disconnected, { type: "event", event: { kind: "stopped", reason } }) }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New call" })).not.toBeInTheDocument();
    expect(ipc.apiCalls().filter(c => c.path === "/sessions/s1")).toHaveLength(0);
  });
  it("shows review for a call that ends now, but doesn't reopen it after New call or remount", async () => {
    routes();
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ id: "s1", status: "ready", mode: "interview", store_transcript: false, questions: 1, outputs: { summary: "The call is complete." }, notes: "" }));
    const live = { ...initial, running: true, sessionId: "s1" };
    const ended = { ...initial, sessionId: "s1", stopped: "ended" as const };
    const view = render(tree({ session: live }));
    view.rerender(tree({ session: ended }));
    await screen.findByText("The call is complete.");
    await userEvent.click(screen.getByRole("button", { name: "New call" }));
    view.rerender(tree({ session: { ...ended } }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    view.unmount();
    render(tree({ session: ended }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeInTheDocument();
    expect(screen.queryByText("The call is complete.")).not.toBeInTheDocument();
  });
});


describe("call status notifications", () => {
  const seat = { sessionId: "guest-call", seatId: "guest-seat", hostName: "Dana", mode: "meeting" as const, title: null, capabilities: [], hostConnected: true, sharing: false };
  const ended = { ...initial, sessionId: seat.sessionId, stopped: "ended" as const, seat };
  it("does not redisplay an ended guest snapshot on startup", async () => {
    routes();
    render(tree({ session: ended }));
    expect(await screen.findByRole("button", { name: "Start call" })).toBeVisible();
    expect(screen.queryByText("Dana's call ended")).not.toBeInTheDocument();
  });
  it("floats a new guest-ended notice once, supports dismissal, and resets for the next call", async () => {
    routes();
    const view = render(tree());
    view.rerender(tree({ session: ended }));
    const notice = await screen.findByText("Dana's call ended");
    expect(notice.closest(".ui-toast-viewport")).not.toBeNull();
    expect(notice.closest(".session-window")).toBeNull();
    fireEvent.click(within(notice.closest(".ui-toast") as HTMLElement).getByRole("button", { name: "Dismiss" }));
    view.rerender(tree({ session: { ...ended }, active: false }));
    view.rerender(tree({ session: { ...ended }, active: true }));
    expect(screen.queryByText("Dana's call ended")).not.toBeInTheDocument();
    view.rerender(tree({ session: initial }));
    view.rerender(tree({ session: { ...ended, sessionId: "next", seat: { ...seat, sessionId: "next", seatId: "next-seat" } } }));
    expect(await screen.findByText("Dana's call ended")).toBeVisible();
  });
  it("expires informational stop notices and suppresses a stale event on profile changes", async () => {
    vi.useFakeTimers();
    try {
      routes();
      const view = render(tree());
      view.rerender(tree({ session: ended }));
      expect(screen.getByText("Dana's call ended")).toBeVisible();
      await act(async () => vi.advanceTimersByTimeAsync(5100));
      expect(screen.queryByText("Dana's call ended")).not.toBeInTheDocument();
      view.rerender(tree({ session: ended, settings: { ...SETTINGS, profile: "other-profile" } }));
      expect(screen.queryByText("Dana's call ended")).not.toBeInTheDocument();
      view.unmount();
    } finally { vi.useRealTimers(); }
  });
});
