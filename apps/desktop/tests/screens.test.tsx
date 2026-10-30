// The rebuilt main-window screens: each list's loading, error and empty states, inline form
// checks, confirmations before deleting, Settings saving on change, and hotkey recording.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { Applications, checkApplication, filterApplications } from "../src/Applications";
import { CallScreen, type CallProps } from "../src/CallScreen";
import { clashWith, duplicateHotkeys, prettyKey, recordKey } from "../src/hotkeys";
import { Resumes } from "../src/Resumes";
import { initial, type SessionView } from "../src/session";
import { SettingsScreen } from "../src/SettingsScreen";
import { ToastProvider } from "../src/ui";
import { Work, checkEmployer } from "../src/Work";
import { app, detail, employer, interview, resume, SETTINGS, settingsView } from "./fixtures";
import { ipc, pending } from "./ipc";

const wrap = (ui: ReactNode) => render(<ToastProvider>{ui}</ToastProvider>);

// --- Hotkeys ----------------------------------------------------------------------------------

describe("hotkeys", () => {
  const key = (code: string, mods: Partial<{ ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean }> = {}) => ({
    code,
    key: code,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    ...mods,
  });
  it("records a combination in the accelerator form", () => {
    expect(recordKey(key("KeyH", { ctrlKey: true, shiftKey: true }))).toEqual({ accel: "CommandOrControl+Shift+H" });
    expect(recordKey(key("ArrowUp", { altKey: true }))).toEqual({ accel: "Alt+Up" });
    expect(recordKey(key("F8"))).toEqual({ accel: "F8" });
    expect(recordKey(key("ShiftLeft", { shiftKey: true }))).toEqual({ waiting: true });
    expect(recordKey(key("KeyA"))).toHaveProperty("error");
  });
  it("reads an accelerator", () => {
    expect(prettyKey("CommandOrControl+Shift+Up")).toBe("Ctrl+Shift+↑");
  });
  it("finds a key already in use", () => {
    expect(clashWith(SETTINGS, "Ctrl+Shift+O", "hotkey")?.action).toBe("toggle_overlay");
    expect(clashWith(SETTINGS, "Ctrl+Shift+O", "toggle_overlay_hotkey")).toBeNull();
    expect(duplicateHotkeys({ ...SETTINGS, hotkey: "CommandOrControl+Shift+T" })).toEqual(new Set(["hotkey", "click_through_hotkey"]));
  });
});

// --- Applications -----------------------------------------------------------------------------

function appRoutes(apps = [app("a1", "Northwind", "Backend", "interview"), app("a2", "Lumen", "Payments")], ivs = [interview("i1", "a1", "Northwind", 60)]) {
  ipc.api("GET", /^\/applications$/, () => ({ applications: apps }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: ivs }));
  ipc.api("GET", /^\/applications\/[^/]+$/, (path) => detail(apps.find((a) => path.endsWith(a.id)) ?? apps[0]));
  ipc.api("GET", /^\/career-os\/connection$/, () => ({ connected: true, available: true }));
}

describe("Applications", () => {
  it("loading: a skeleton", () => {
    ipc.api("GET", /.*/, () => pending());
    wrap(<Applications />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading applications");
  });

  it("error: says what failed, Retry loads again", async () => {
    let fail = true;
    appRoutes();
    ipc.api("GET", /^\/applications$/, () => {
      if (fail) throw "Gateway down (HTTP 502)";
      return { applications: [app("a1", "Northwind", "Backend")] };
    });
    wrap(<Applications />);
    expect(await screen.findByText("Gateway down (HTTP 502)")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /^Northwind/ })).toBeInTheDocument();
  });

  it("empty: says what to do, with Add application", async () => {
    appRoutes([], []);
    wrap(<Applications />);
    expect(await screen.findByRole("heading", { name: "No applications yet" })).toBeInTheDocument();
  });

  it("lists, filters by words and stage, and starts a call for an upcoming interview", async () => {
    appRoutes();
    let started: string | null = null;
    wrap(<Applications onStartInterview={(id) => (started = id)} />);
    const list = await screen.findByRole("list", { name: "All applications" });
    expect(within(list).getAllByRole("button")).toHaveLength(2);
    await userEvent.type(screen.getByRole("searchbox", { name: "Search company or role" }), "lum");
    expect(within(list).getAllByRole("button")).toHaveLength(1);
    await userEvent.clear(screen.getByRole("searchbox"));
    await userEvent.click(screen.getByLabelText("Interviewing"));
    expect(within(screen.getByRole("list", { name: "All applications" })).getByText("Northwind")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Start a call for Northwind/ }));
    expect(started).toBe("i1");
  });

  it("the Add dialog checks fields inline and adds nothing until they pass", async () => {
    appRoutes();
    ipc.api("POST", /^\/applications$/, (_p, body) => detail({ ...app("a9", (body as { company: string }).company, "Role"), id: "a9" }));
    wrap(<Applications />);
    await userEvent.click(await screen.findByRole("button", { name: "+ Add application" }));
    const dialog = screen.getByRole("dialog", { name: "Add application" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Add application" }));
    expect(within(dialog).getByText("Enter the company.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Company")).toHaveAttribute("aria-invalid", "true");
    expect(ipc.apiCalls("POST")).toHaveLength(0);
    await userEvent.type(within(dialog).getByLabelText("Company"), "Cedar");
    await userEvent.type(within(dialog).getByLabelText("Role"), "Engineer");
    await userEvent.type(within(dialog).getByLabelText("Job description"), "Build things.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add application" }));
    await waitFor(() => expect(ipc.apiCalls("POST")).toHaveLength(1));
    expect(ipc.apiCalls("POST")[0].body).toMatchObject({ company: "Cedar", role: "Engineer", jd_text: "Build things." });
  });

  it("deleting asks first", async () => {
    appRoutes();
    ipc.api("DELETE", /^\/applications\/a1$/, () => null);
    wrap(<Applications />);
    await userEvent.click(await screen.findByRole("button", { name: /^Northwind/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Application actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete application…" }));
    expect(ipc.apiCalls("DELETE")).toHaveLength(0);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete application" }));
    await waitFor(() => expect(ipc.apiCalls("DELETE")).toHaveLength(1));
  });

  it("helpers: field checks and filtering", () => {
    expect(checkApplication({ company: "", role: "x", url: "nope", source_site: null, stage: "applied", note: null, jd_text: "y" })).toMatchObject({ company: "Enter the company.", url: expect.any(String) });
    expect(filterApplications([app("a", "Northwind Freight", "Backend"), app("b", "Lumen", "Backend", "offer")], "backend north", "all").map((a) => a.id)).toEqual(["a"]);
    expect(filterApplications([app("a", "Northwind", "Backend"), app("b", "Lumen", "Backend", "offer")], "", "offer").map((a) => a.id)).toEqual(["b"]);
  });
});

// --- Resumes ----------------------------------------------------------------------------------

describe("Resumes", () => {
  it("loading, then an error with Retry", async () => {
    let fail = true;
    ipc.api("GET", /^\/resumes$/, () => {
      if (fail) throw "HTTP 500";
      return { base: resume("rb", "base", "Alex 2026"), versions: [] };
    });
    wrap(<Resumes />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading resumes");
    expect(await screen.findByText("HTTP 500")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Alex 2026")).toBeInTheDocument();
  });

  it("empty: asks for the base resume; tailored says where they come from", async () => {
    ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [] }));
    wrap(<Resumes />);
    expect(await screen.findByRole("heading", { name: "Add your base resume" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No tailored resumes" })).toBeInTheDocument();
  });

  it("deleting a tailored resume asks first", async () => {
    ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex 2026"), versions: [resume("rt", "tailored", "Northwind v2", { id: "a1", company: "Northwind", role: "Backend" })] }));
    ipc.api("DELETE", /^\/resumes\/rt$/, () => null);
    wrap(<Resumes />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for Northwind v2" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Northwind then uses its next resume");
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete resume" }));
    await waitFor(() => expect(ipc.apiCalls("DELETE")[0].path).toBe("/resumes/rt"));
  });

  it("replacing needs text or a file", async () => {
    ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex 2026"), versions: [] }));
    wrap(<Resumes />);
    await userEvent.click(await screen.findByRole("button", { name: "Replace…" }));
    await userEvent.click(screen.getByRole("button", { name: "Save pasted text" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Paste the resume's text");
    expect(ipc.apiCalls("POST")).toHaveLength(0);
  });
});

// --- Work -------------------------------------------------------------------------------------

function workRoutes(list = [employer("e1", "Fernhill")]) {
  ipc.api("GET", /^\/employers$/, () => ({ employers: list }));
  ipc.api("GET", /^\/employers\/[^/]+\/logs$/, () => ({ logs: [], next_before: null }));
  ipc.api("GET", /^\/employers\/[^/]+\/documents$/, () => ({ documents: [] }));
  ipc.api("GET", /^\/employers\/[^/]+\/memory$/, () => ({ memory: [] }));
  ipc.api("GET", /^\/proposals$/, () => ({ proposals: [] }));
}

describe("Work", () => {
  it("loading and error states", async () => {
    let fail = true;
    workRoutes();
    ipc.api("GET", /^\/employers$/, () => {
      if (fail) throw "HTTP 503";
      return { employers: [employer("e1", "Fernhill")] };
    });
    wrap(<Work />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading companies");
    expect(await screen.findByText("HTTP 503")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("tab", { name: "Log" })).toBeInTheDocument();
  });

  it("empty: Add company opens a form that checks the name", async () => {
    workRoutes([]);
    ipc.api("POST", /^\/employers$/, () => employer("e2", "Cedar"));
    wrap(<Work />);
    await userEvent.click(await screen.findByRole("button", { name: "Add company" }));
    const dialog = screen.getByRole("dialog", { name: "Add a company" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Add company" }));
    expect(within(dialog).getByText("Enter the company's name.")).toBeInTheDocument();
    expect(ipc.apiCalls("POST")).toHaveLength(0);
    await userEvent.type(within(dialog).getByLabelText("Company"), "Cedar");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add company" }));
    await waitFor(() => expect(ipc.apiCalls("POST")).toHaveLength(1));
  });

  it("archive offers Undo", async () => {
    workRoutes();
    ipc.api("PATCH", /^\/employers\/e1$/, (_p, body) => employer("e1", "Fernhill", body as object));
    wrap(<Work />);
    await userEvent.click(await screen.findByRole("button", { name: "Company actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /Archive/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(ipc.apiCalls("PATCH")).toHaveLength(2));
    expect(ipc.apiCalls("PATCH").map((c) => c.body)).toEqual([{ status: "archived" }, { status: "active" }]);
  });

  it("deleting all of a company's data needs its name typed", async () => {
    workRoutes();
    ipc.api("DELETE", /^\/employers\/e1$/, () => ({ deleted: {} }));
    wrap(<Work />);
    await userEvent.click(await screen.findByRole("button", { name: "Company actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete all its data…" }));
    const confirm = within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete all data" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type Fernhill to confirm/), "fernhill");
    await userEvent.click(confirm);
    await waitFor(() => expect(ipc.apiCalls("DELETE")[0].path).toBe("/employers/e1?confirm=Fernhill"));
  });

  it("field checks", () => {
    expect(checkEmployer({ name: " ", started: "", ended: "" })).toEqual({ name: "Enter the company's name." });
    expect(checkEmployer({ name: "A", started: "2024-05-01", ended: "2024-01-01" })).toEqual({ ended: "The end can't be before the start." });
  });
});

// --- Settings ---------------------------------------------------------------------------------

describe("Settings", () => {
  const props = () => ({
    view: settingsView(),
    running: false,
    onSaved: () => {},
    update: { state: "none" } as const,
    check: { busy: false, error: null, start: () => {} },
  });

  it("a switch saves at once", async () => {
    ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Audio" }));
    await userEvent.click(screen.getByRole("switch", { name: "Echo cancellation" }));
    const saves = ipc.calls.filter((c) => c.cmd === "save_settings");
    expect(saves).toHaveLength(1);
    expect((saves[0].args.settings as typeof SETTINGS).echo_cancellation).toBe(false);
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("the theme switches at once and is kept", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByLabelText("Dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("copilot.theme")).toBe("dark");
  });

  it("a hotkey is recorded by pressing it, and a key in use is refused", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Overlay & hotkeys" }));
    const row = screen.getByText("Answer the last thing said").closest(".ui-hotkey") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    const box = within(row).getByRole("textbox");
    fireEvent.keyDown(box, { code: "KeyO", key: "o", ctrlKey: true, shiftKey: true });
    expect(await within(row).findByRole("alert")).toHaveTextContent("“Hide or show the overlay” already uses that key");
    expect(ipc.calls.filter((c) => c.cmd === "save_settings")).toHaveLength(0);
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    fireEvent.keyDown(within(row).getByRole("textbox"), { code: "KeyJ", key: "j", ctrlKey: true, altKey: true });
    await waitFor(() => expect(ipc.calls.filter((c) => c.cmd === "save_settings")).toHaveLength(1));
    expect((ipc.calls.find((c) => c.cmd === "save_settings")!.args.settings as typeof SETTINGS).hotkey).toBe("CommandOrControl+Alt+J");
  });

  it("the fallback hotkey cannot be cleared", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Overlay & hotkeys" }));
    expect(screen.queryByRole("button", { name: "Clear Turn click-through off" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear Answer the last thing said" })).toBeInTheDocument();
  });
});

// --- Call -------------------------------------------------------------------------------------

function callProps(over: Partial<CallProps> = {}): CallProps {
  return {
    session: initial,
    tokenSet: true,
    environment: "cloud",
    release: true,
    signedInAs: "alex@example.com",
    settings: SETTINGS,
    careerOs: { connected: true, available: true },
    locked: false,
    onCareerOsChanged: () => {},
    onSettingsSaved: () => {},
    onSettings: () => {},
    onResumes: () => {},
    onOpenLive: async () => {},
    ...over,
  };
}

function callRoutes() {
  appRoutes();
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex"), versions: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
}

describe("Call", () => {
  it("signed out: the setup steps, Sign in first, Start waits", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null })} />);
    expect(screen.getByRole("heading", { name: "Get ready for your first call" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start call" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    expect(ipc.calls.some((c) => c.cmd === "sign_in")).toBe(true);
  });

  it("locked: the Career-OS step asks for the code", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps({ locked: true, careerOs: { connected: false, available: true, required: true } })} />);
    expect(screen.getByRole("heading", { name: /Sign in.*done/ })).toBeInTheDocument();
    expect(screen.getByLabelText("One-time code")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Paste the whole code");
  });

  it("ready: the next interview is chosen, and Start call starts it", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps()} />);
    expect(await screen.findByRole("heading", { name: /Northwind · Engineer/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    const start = ipc.calls.find((c) => c.cmd === "start_session");
    expect(start?.args.request).toMatchObject({ mode: "interview", interview_id: "i1", job_id: "a1", store_transcript: true });
  });

  it("live: End call asks how to save it, then ends with a summary", async () => {
    callRoutes();
    const live: SessionView = { ...initial, running: true, sessionId: "s1", connection: { state: "live", session_id: "s1", resumed: false } };
    wrap(<CallScreen {...callProps({ session: live })} />);
    await userEvent.click(screen.getByRole("button", { name: "End call…" }));
    const dialog = screen.getByRole("alertdialog", { name: "End this call?" });
    expect(within(dialog).getByRole("button", { name: "End, transcript only" })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Summarize and end" }));
    await waitFor(() => expect(ipc.calls.find((c) => c.cmd === "end_session")?.args).toEqual({ summarize: true }));
  });

  it("a call refused because another runs says so, with a way out", async () => {
    callRoutes();
    ipc.handle("end_other_session", () => "Ended the other call");
    wrap(<CallScreen {...callProps({ session: { ...initial, stopped: "session_active" } })} />);
    expect(await screen.findByText("Another call is active")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End the other call" }));
    expect(await screen.findByText(/You can start now/)).toBeInTheDocument();
  });
});

// --- Accessibility ----------------------------------------------------------------------------

describe("accessibility", () => {
  const axeRun = async () => {
    const axe = (await import("axe-core")).default;
    const r = await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
    return r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  };
  it("Call, Applications, Resumes, Work and Settings have no axe violations", async () => {
    callRoutes();
    workRoutes();
    ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex 2026"), versions: [resume("rt", "tailored", "Northwind v2", { id: "a1", company: "Northwind", role: "Backend" })] }));
    const screensToCheck: [string, ReactNode, () => Promise<unknown>][] = [
      ["Call", <CallScreen {...callProps()} />, () => screen.findByRole("heading", { name: /Northwind · Engineer/ })],
      ["Call setup", <CallScreen {...callProps({ tokenSet: false, careerOs: null })} />, () => screen.findByRole("heading", { name: "Get ready for your first call" })],
      ["Applications", <Applications />, () => screen.findByRole("tab", { name: "Details" })],
      ["Resumes", <Resumes />, () => screen.findByText("Northwind v2")],
      ["Work", <Work />, () => screen.findByRole("tab", { name: "Log" })],
      ["Settings", <SettingsScreen view={settingsView()} running={false} onSaved={() => {}} update={{ state: "none" }} check={{ busy: false, error: null, start: () => {} }} />, () => screen.findByRole("heading", { name: "Settings" })],
    ];
    for (const [name, ui, ready] of screensToCheck) {
      const { unmount } = wrap(ui);
      await ready();
      expect({ name, violations: await axeRun() }).toEqual({ name, violations: [] });
      unmount();
    }
  });
});
