import { emit } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import type { Numbered, Profile, SessionDetail } from "../src/api";
import { useProfiles, type ProfileState } from "../src/profiles";
import { initial, postCallFor, useSession } from "../src/session";
import { settingsView } from "./fixtures";
import { ipc } from "./ipc";

// Keep the real App, profile/session hooks and call screens. Only the shell's window
// controls are replaced so the test can switch profiles even on the ended-call screen.
vi.mock("../src/Shell", () => ({
  Shell: ({ children, profiles, onSwitchProfile, profileError }: { children: ReactNode; profiles: ProfileState; onSwitchProfile: (id: string | null) => void; profileError: string | null }) => <>
    <output aria-label="Current profile">{profiles.here}</output>
    {profiles.profiles.map(profile => <button key={profile.id} onClick={() => onSwitchProfile(profile.id)}>Use {profile.name}</button>)}
    {profileError && <div role="alert">{profileError}</div>}
    {children}
  </>,
  WorkspaceMenu: () => null,
  WorkspaceProfileSwitch: () => null,
  useWorkspaceCompactWindow: () => {},
}));

// Load the real lazy screens before interaction deadlines; cold Vite transforms can
// exceed Testing Library's default wait on a busy CI runner.
beforeAll(async () => {
  await Promise.all([import("../src/SessionDetail"), import("../src/SettingsScreen")]);
});

const me: Profile = { id: "account", name: "Dana", me: true, career_os_user_id: null };
const other: Profile = { id: "other", name: "Alex", me: false, career_os_user_id: "alex" };
const ended: Numbered[] = [
  { n: 0, event: { kind: "connection", state: { state: "live", session_id: "previous-call", resumed: false } } },
  { n: 1, event: { kind: "stopped", reason: "ended" } },
];
const detail: SessionDetail = {
  id: "previous-call", mode: "interview", title: null, platform: null,
  job_id: null, interview_id: null, employer_id: null, store_transcript: false,
  started_at: "2026-11-18T18:00:00Z", ended_at: "2026-11-18T18:10:00Z",
  billed_seconds: 600, me_audio_status: "live", status: "ready",
  outputs: { summary: "The previous profile's completed call." }, questions: 0,
};

function appRoutes(failSwitch = false) {
  mockWindows("main");
  let selected: string | null = null;
  const requestedFor: (string | null)[] = [];
  ipc.handle("get_settings", () => settingsView());
  ipc.handle("snapshot", () => [ended, false, 2]);
  ipc.handle("update_status", () => ({ state: "none" }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
  ipc.handle("set_profile", ({ profile }) => {
    if (failSwitch) throw "end the session first";
    selected = profile as string | null;
    // Deliberately delay the native context_reset event: App must clear the previous
    // session before its keyed profile screens remount, independently of event timing.
  });
  ipc.api("GET", /^\/profiles$/, () => ({ items: [me, other] }));
  ipc.api("GET", /^\/career-os\/connection$/, () => ({ connected: true, available: true }));
  ipc.api("GET", /^\/applications$/, () => ({ applications: [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/sessions\/previous-call$/, () => {
    requestedFor.push(selected);
    if (selected !== null) throw "Session not found (HTTP 404)";
    return detail;
  });
  return requestedFor;
}

describe("profile session boundaries", () => {
  it("returns to setup after switching profiles without opening the previous profile's ended call", async () => {
    const requestedFor = appRoutes();
    render(<App />);
    expect(await screen.findByText(detail.outputs.summary!)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Use Alex" }));
    await waitFor(() => expect(screen.getByLabelText("Current profile")).toHaveTextContent("other"));
    expect(await screen.findByRole("button", { name: /^Start call/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Call ended/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/This session no longer exists/)).not.toBeInTheDocument();
    expect(requestedFor).not.toContain("other");
    const requestsAfterSwitch = requestedFor.length;
    await userEvent.click(screen.getByRole("button", { name: "Use Dana" }));
    await waitFor(() => expect(screen.getByLabelText("Current profile")).toHaveTextContent("account"));
    expect(screen.queryByRole("heading", { name: /^Call ended/ })).not.toBeInTheDocument();
    expect(requestedFor).toHaveLength(requestsAfterSwitch);
  });

  it("keeps the current profile and ended-call screen when native switching fails", async () => {
    appRoutes(true);
    render(<App />);
    expect(await screen.findByText(detail.outputs.summary!)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Use Alex" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("end the session first");
    expect(screen.getByLabelText("Current profile")).toHaveTextContent("account");
    expect(screen.getByRole("heading", { name: /^Call ended/ })).toBeInTheDocument();
    expect(screen.getByText(detail.outputs.summary!)).toBeInTheDocument();
  });

  it("does not reset the selected profile when an automatic fallback is refused during a call", async () => {
    ipc.api("GET", /^\/profiles$/, () => { throw "Profile unavailable (HTTP 403)"; });
    ipc.handle("set_profile", () => { throw "end the session first"; });
    const changed = vi.fn();
    const { result } = renderHook(() => useProfiles(true, "other", "cloud:dana", changed));
    await waitFor(() => expect(ipc.calls.some(call => call.cmd === "set_profile")).toBe(true));
    expect(result.current.state.current).toBe("other");
    expect(changed).not.toHaveBeenCalled();
  });

  it("applies a context reset after a delayed old snapshot, ignores stale events, and accepts the next call", async () => {
    let complete!: (snapshot: [Numbered[], boolean, number]) => void;
    ipc.handle("snapshot", () => new Promise(resolve => { complete = resolve; }));
    const { result } = renderHook(useSession);
    await waitFor(() => expect(ipc.calls.some(call => call.cmd === "snapshot")).toBe(true));
    await act(() => emit("copilot://event", { n: 2, event: { kind: "context_reset" } }));
    await act(async () => complete([ended, false, 2]));
    expect(result.current[0]).toEqual(initial);
    expect(postCallFor(result.current[0])).toBeNull();
    await act(() => emit("copilot://event", ended[1]));
    expect(result.current[0]).toEqual(initial);
    await act(() => emit("copilot://event", { n: 0, event: { kind: "connection", state: { state: "live", session_id: "new-call", resumed: false } } }));
    expect(result.current[0]).toMatchObject({ running: true, sessionId: "new-call", stopped: null });
  });

  it("restores an empty session from the native snapshot after a profile switch", async () => {
    ipc.handle("snapshot", () => [[{ n: 87, event: { kind: "context_reset" } }], false, 88]);
    const { result } = renderHook(useSession);
    await act(async () => {});
    expect(result.current[0]).toEqual(initial);
    expect(postCallFor(result.current[0])).toBeNull();
  });
});

describe("native call navigation", () => {
  it("returns from Settings to the current call and retains updates received while hidden", async () => {
    appRoutes();
    const liveEvents: Numbered[] = [
      { n: 0, event: { kind: "connection", state: { state: "live", session_id: "active-call", resumed: false } } },
      { n: 1, event: { kind: "server", message: { seq: 1, type: "question.detected", question_id: "q1", text: "How did you recover?", question_type: "behavioral", confidence: 0.9, speculative: false } } },
    ];
    ipc.handle("snapshot", () => [liveEvents, true, 2]);
    ipc.handle("live_status", () => ({ session_id: "active-call", levels: null, stats: null }));
    render(<App />);
    const question = await screen.findByText('"How did you recover?"');
    const endCall = screen.getByRole("button", { name: "End call…" });
    await waitFor(() => expect(screen.getByLabelText("Current profile")).toHaveTextContent("account"));

    await act(() => emit("copilot://open-settings"));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
    expect(endCall).not.toBeVisible();
    expect(question).not.toBeVisible();
    await act(() => emit("copilot://event", {
      n: 2,
      event: { kind: "server", message: { seq: 2, type: "question.detected", question_id: "q2", text: "What did you learn?", question_type: "behavioral", confidence: 0.9, speculative: false } },
    } satisfies Numbered));

    await act(() => emit("copilot://open-call"));
    expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "End call…" })).toBeVisible();
    expect(await screen.findByText('"What did you learn?"')).toBeVisible();
    expect(screen.getByLabelText("Current profile")).toHaveTextContent("account");
    expect(screen.queryByRole("button", { name: /^Start call/ })).not.toBeInTheDocument();
    expect(ipc.calls.filter(call => call.cmd === "snapshot")).toHaveLength(1);
    expect(ipc.calls.some(call => ["start_session", "open_session", "end_session", "set_profile"].includes(call.cmd))).toBe(false);
  });
});
