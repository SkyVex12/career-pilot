import { mockWindows } from "@tauri-apps/api/mocks";
// The rebuilt main-window screens: each list's loading, error and empty states, inline form
// checks, confirmations before deleting, Settings saving on change, and hotkey recording.
import { emit } from "@tauri-apps/api/event";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { LiveStatus } from "../src/api";
import { Applications, checkApplication, filterApplications, listStamp } from "../src/Applications";
import { CallScreen, type CallProps } from "../src/CallScreen";
import { Shell } from "../src/Shell";
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
    expect(clashWith(SETTINGS, "Ctrl+Shift+S", "hotkey")?.action).toBe("toggle_stealth");
    expect(clashWith(SETTINGS, "Ctrl+Shift+S", "toggle_stealth_hotkey")).toBeNull();
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
  it("the open view's stamp changes when its pack is built, it is pulled again, or its resume changes", () => {
    const a = app("a1", "Northwind", "Backend");
    const building = { ...a, pack: null };
    expect(listStamp(undefined)).toBe("");
    expect(listStamp(building)).not.toBe(listStamp(a));
    expect(listStamp({ ...a, career_os: { application_id: "c1", pulled_at: "2026-11-17T10:00:00Z", content_hash: null } })).not.toBe(listStamp(a));
    expect(listStamp({ ...a, resume: { ...a.resume!, status: "extracting" } })).not.toBe(listStamp(a));
    expect(listStamp({ ...a, resume: { ...a.resume!, label: "Renamed default resume" } })).not.toBe(listStamp(a));
    expect(listStamp({ ...a, note: "only in the view's form" })).toBe(listStamp(a));
  });

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

  it("lists every stage, filters by words, and starts a call for an upcoming interview", async () => {
    appRoutes();
    let started: string | null = null;
    wrap(<Applications onStartInterview={(id) => (started = id)} />);
    const list = await screen.findByRole("list", { name: "All applications" });
    expect(within(list).getAllByRole("button")).toHaveLength(2);
    await userEvent.type(screen.getByRole("searchbox", { name: "Search company or role" }), "lum");
    expect(within(list).getAllByRole("button")).toHaveLength(1);
    await userEvent.clear(screen.getByRole("searchbox"));
    expect(screen.queryByRole("radio", { name: "Interviewing" })).not.toBeInTheDocument();
    expect(within(list).getAllByRole("button")).toHaveLength(2);
    expect(within(screen.getByRole("list", { name: "All applications" })).getByText("Northwind")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Upcoming interviews" })).not.toBeInTheDocument();
    await userEvent.click(within(list).getByRole("button", { name: /^Northwind/ }));
    await userEvent.click(screen.getByRole("button", { name: /Start a call for Northwind/ }));
    expect(started).toBe("i1");
  });

  it("the Add dialog checks fields inline and adds nothing until they pass", async () => {
    appRoutes();
    ipc.api("POST", /^\/applications$/, (_p, body) => detail({ ...app("a9", (body as { company: string }).company, "Role"), id: "a9" }));
    wrap(<Applications />);
    await userEvent.click(await screen.findByRole("button", { name: "+ Add application" }));
    const dialog = screen.getByRole("dialog", { name: "Add application" });
    expect(within(dialog).queryByLabelText("Stage")).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/More details/)).not.toBeInTheDocument();
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
    expect(filterApplications([app("a", "Northwind Freight", "Backend"), app("b", "Lumen", "Backend", "offer")], "backend north").map((a) => a.id)).toEqual(["a"]);
    expect(filterApplications([app("a", "Northwind", "Backend"), app("b", "Lumen", "Backend", "offer")], "").map((a) => a.id)).toEqual(["a", "b"]);
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
    expect(await screen.findByRole("tab", { name: "Work log" })).toBeInTheDocument();
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

  it("engineering context: each file's status, what it gave, and a test question with its sources", async () => {
    workRoutes();
    const doc = { content_type: "text/markdown", size_bytes: 10, created_at: new Date().toISOString(), pending_proposals: 0 };
    ipc.api("GET", /^\/employers\/e1\/documents$/, () => ({
      documents: [
        { ...doc, id: "d1", name: "dispatch.md", status: "ingested", sections: 6, facts: 9 },
        { ...doc, id: "d2", name: "scan.md", status: "failed", sections: 0, facts: 0, error: "The file has no text." },
        { ...doc, id: "d3", name: "new.md", status: "ingesting", sections: 0, facts: 0, replaces: "d1" },
      ],
    }));
    ipc.api("GET", /^\/employers\/e1\/documents\/d1$/, () => ({
      id: "d1",
      name: "dispatch.md",
      status: "ingested",
      error: null,
      sections: 6,
      headings: ["Dispatch > Decisions"],
      facts: [
        {
          id: "n1",
          kind: "decision",
          title: "Keep SQS",
          summary: "Keep SQS FIFO for the dispatch queue",
          body: { why: "Ordering per route is enough", alternatives: ["Kafka: nobody runs it"] },
          day: null,
          status: "active",
          source: { type: "company_document", ref: "d1" },
          edited: false,
        },
      ],
    }));
    let polls = 0;
    ipc.api("POST", /^\/employers\/e1\/knowledge\/ask$/, () => ({ ask_id: "a1", status: "pending" }));
    ipc.api("GET", /^\/employers\/e1\/knowledge\/ask\/a1$/, () => {
      polls += 1;
      if (polls < 2) return { ask_id: "a1", status: "pending" };
      return {
        ask_id: "a1",
        status: "done",
        type: "decision",
        answer: "- We kept SQS FIFO\n- Kafka was rejected",
        sources: [{ kind: "section", label: "dispatch.md > Dispatch > Decisions" }],
      };
    });
    wrap(<Work />);
    await userEvent.click(await screen.findByRole("tab", { name: /Documents/ }));
    expect(await screen.findByText("6 sections, 9 facts")).toBeInTheDocument();
    expect(screen.getByText("Could not read it")).toBeInTheDocument();
    expect(screen.getByText(/The file has no text\./)).toBeInTheDocument();
    expect(screen.getByText("Reading")).toBeInTheDocument();
    expect(screen.getByText(/replaces the earlier upload once read/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Actions for dispatch.md" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "What it gave…" }));
    const gave = await screen.findByRole("dialog", { name: "What dispatch.md gave" });
    expect(await within(gave).findByText("Dispatch > Decisions")).toBeInTheDocument();
    expect(within(gave).getByText("Keep SQS: Keep SQS FIFO for the dispatch queue")).toBeInTheDocument();
    expect(within(gave).getByText("Why: Ordering per route is enough")).toBeInTheDocument();
    expect(within(gave).getByText("Rejected: Kafka: nobody runs it")).toBeInTheDocument();
    await userEvent.click(within(gave).getByRole("button", { name: "Done" }));

    const ask = screen.getByRole("button", { name: "Ask" });
    expect(ask).toBeDisabled();
    await userEvent.type(screen.getByLabelText("A question a teammate might ask"), "Why SQS for dispatch?");
    await userEvent.click(ask);
    const answer = await screen.findByLabelText("Answer", {}, { timeout: 5000 });
    expect(ipc.apiCalls("POST").slice(-1)[0].body).toEqual({ question: "Why SQS for dispatch?" });
    expect(within(answer).getByText("Decision")).toBeInTheDocument();
    expect(within(answer).getByText(/We kept SQS FIFO/)).toBeInTheDocument();
    expect(within(answer).getByText("Document: dispatch.md > Dispatch > Decisions")).toBeInTheDocument();
  }, 15000);

  it("field checks", () => {
    expect(checkEmployer({ name: " ", started: "", ended: "" })).toEqual({ name: "Enter the company's name." });
    expect(checkEmployer({ name: "A", started: "2024-05-01", ended: "2024-01-01" })).toEqual({ ended: "The end can't be before the start." });
  });
});

// --- Settings ---------------------------------------------------------------------------------

describe("Settings", () => {
  it("puts concise help beside setting titles and keeps the controls directly usable", async () => {
    wrap(<SettingsScreen view={settingsView()} running={false} onSaved={() => {}} update={{ state: "none" }} check={{ busy: false, error: null, start: () => {} }} />);
    expect(screen.queryByRole("button", { name: /^(Details|Hide details)$/ })).not.toBeInTheDocument();
    const theme = screen.getByRole("button", { name: "About Theme" });
    await userEvent.hover(theme);
    expect(screen.getByRole("tooltip")).toHaveTextContent("System follows Windows. The overlay stays dark.");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "About Stealth mode" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("supported screen captures");
    expect(ipc.calls.some(call => call.cmd === "save_settings")).toBe(false);
    await userEvent.click(screen.getByRole("switch", { name: "Stealth mode" }));
    expect(ipc.calls.some(call => call.cmd === "save_settings")).toBe(true);
  });

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

  it("distinguishes Mac system capture from the selected playback output", async () => {
    const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    try {
      ipc.handle("list_devices", () => ({ inputs: [], outputs: [{ id: "headset", name: "USB headset", is_default: true }] }));
      wrap(<SettingsScreen {...props()} />);
      await userEvent.click(screen.getByRole("button", { name: "Audio" }));
      expect(screen.getByText("Assistant audio and test tone. Transcription captures system audio from all apps.")).toBeVisible();
      expect(screen.queryByRole("combobox", { name: "Call audio from" })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("combobox", { name: "Playback output" }));
      await userEvent.click(await screen.findByRole("option", { name: "USB headset (default)" }));
      const save = ipc.calls.find(call => call.cmd === "save_settings");
      expect((save?.args.settings as typeof SETTINGS).output_device).toBe("headset");
    } finally { platform.mockRestore(); }
  });

  it("the theme switches at once and is kept", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByLabelText("Dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("copilot.theme")).toBe("dark");
  });

  it("a hotkey is recorded by pressing it, and a key in use is refused", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
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

  it("the Stealth shortcut can be rebound without accepting another action's key", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
    const row = screen.getByText("Stealth mode on or off").closest(".ui-hotkey") as HTMLElement;
    expect(within(row).getByText("Ctrl+Shift+S")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    fireEvent.keyDown(within(row).getByRole("textbox"), { code: "KeyG", key: "g", ctrlKey: true, shiftKey: true });
    expect(await within(row).findByRole("alert")).toHaveTextContent("Turn click-through off");
    expect(ipc.calls.filter(call => call.cmd === "save_settings")).toHaveLength(0);
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    fireEvent.keyDown(within(row).getByRole("textbox"), { code: "F9", key: "F9", altKey: true });
    await waitFor(() => expect(ipc.calls.filter(call => call.cmd === "save_settings")).toHaveLength(1));
    expect((ipc.calls.find(call => call.cmd === "save_settings")!.args.settings as typeof SETTINGS).toggle_stealth_hotkey).toBe("Alt+F9");
  });

  it("the fallback hotkey cannot be cleared", async () => {
    wrap(<SettingsScreen {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
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

function wrapCallWindow(ui: ReactNode) {
  return wrap(<Shell tab="start" onTab={() => {}} session={initial} profiles={{ profiles: [], here: null, current: null }} onSwitchProfile={() => {}} profileError={null} version={null} showDebug={false}>{ui}</Shell>);
}
async function collapseToMedium() {
  await userEvent.click(screen.getByRole("button", { name: "Medium window" }));
}
function callRoutes() {
  appRoutes();
  ipc.handle("live_status", (): LiveStatus => ({ running: false, session_id: null, levels: null, stats: null }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|cursor_position", () => ({ x: 10000, y: 10000 }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex"), versions: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
}

describe("Call", () => {
  it("adds an application from call setup and returns with it selected", async () => {
    callRoutes();
    const added = app("new-job", "Cedar", "Platform engineer");
    let saved = false;
    ipc.api("GET", /^\/applications$/, () => ({ applications: saved ? [added] : [] }));
    ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
    ipc.api("POST", /^\/applications$/, () => { saved = true; return detail(added); });
    wrap(<CallScreen {...callProps()} />);
    const transcript = await screen.findByRole("checkbox", { name: "Save transcript" });
    await userEvent.click(transcript);
    await userEvent.click(within(screen.getByRole("combobox", { name: "Application" }).closest(".call-row")!).getByRole("button", { name: "Add" }));
    const dialog = screen.getByRole("dialog", { name: "Add application" });
    await userEvent.type(within(dialog).getByLabelText("Company"), "Cedar");
    await userEvent.type(within(dialog).getByLabelText("Role"), "Platform engineer");
    await userEvent.type(within(dialog).getByLabelText("Job description"), "Build reliable services.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add application" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add application" })).not.toBeInTheDocument());
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("new-job");
    expect(transcript).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    await waitFor(() => expect(ipc.calls.some(call => call.cmd === "start_session")).toBe(true));
    expect(ipc.calls.find(call => call.cmd === "start_session")?.args).toMatchObject({ request: { job_id: "new-job", store_transcript: false } });
  });

  it("signed out: the setup steps, Sign in first, Start waits", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null })} />);
    expect(screen.getByRole("heading", { name: "Get ready for your first call" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start call" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(ipc.calls.some((c) => c.cmd === "sign_in")).toBe(true);
  });

  it("email sign-in: a code, a resend countdown, then a wrong code and an expired one", async () => {
    callRoutes();
    ipc.handle("email_sign_in_start", () => ({ code_length: 6, expires_in_s: 600, resend_after_s: 60 }));
    let tries = 0;
    ipc.handle("email_sign_in_verify", () => {
      tries += 1;
      throw tries === 1
        ? { code: "invalid_code", message: "no" }
        : { code: "code_expired", message: "no" };
    });
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null, settings: { ...SETTINGS, email_sign_in: true } })} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with email" }));
    await userEvent.type(screen.getByLabelText("Email address"), "sam@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/We sent a 6-digit code to/)).toBeInTheDocument();
    expect(screen.getByText("sam@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Send a new code in \d+:\d+/ })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Code"), "123 456");
    expect(screen.getByLabelText("Code")).toHaveValue("123456");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code isn't right");
    await userEvent.type(screen.getByLabelText("Code"), "654321");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code has expired or was already used");
    const start = ipc.calls.find((c) => c.cmd === "email_sign_in_start");
    expect(start?.args.email).toBe("sam@example.com");
  });

  it("HubRuby sign-in: several accounts, then an email that matches", async () => {
    callRoutes();
    ipc.handle("career_os_sign_in", () => ({
      status: "choose_account",
      ticket: "tkt",
      expires_in_s: 600,
      accounts: [
        { id: "a", email: "s•••@x.com", methods: ["google"], created_at: "2026-01-02" },
        { id: "b", email: "t•••@x.com", methods: ["email"], created_at: "2026-03-04" },
      ],
    }));
    ipc.handle("career_os_complete", () => ({ status: "added" }));
    ipc.handle("sign_in", () => "sam@example.com");
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null, settings: { ...SETTINGS, career_os_sign_in: true } })} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with HubRuby" }));
    expect(await screen.findByText(/more than one account/)).toBeInTheDocument();
    expect(screen.getByText(/s•••@x.com/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Use this account" })[0]);
    expect(screen.getByText(/Sign in to that account with Google or email/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    await waitFor(() => expect(ipc.calls.some((c) => c.cmd === "career_os_complete")).toBe(true));
    const done = ipc.calls.find((c) => c.cmd === "career_os_complete");
    expect(done?.args.choice).toBe("existing");
    expect(done?.args.accountId ?? done?.args.account_id).toBe("a");
  });

  it("HubRuby sign-in: an unverified email offers the existing account or a new one", async () => {
    callRoutes();
    ipc.handle("career_os_sign_in", () => ({
      status: "existing_or_new",
      ticket: "tkt",
      expires_in_s: 600,
      verified: false,
      account: null,
    }));
    ipc.handle("career_os_complete", () => ({ status: "signed_in", name: "Sam", account: "new" }));
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null, settings: { ...SETTINGS, career_os_sign_in: true } })} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with HubRuby" }));
    expect(await screen.findByText(/only a hint/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create a separate account" }));
    await waitFor(() => expect(ipc.calls.some((c) => c.cmd === "career_os_complete")).toBe(true));
    expect(ipc.calls.find((c) => c.cmd === "career_os_complete")?.args.choice).toBe("new");
  });

  it("HubRuby being down leaves the other buttons and says why", async () => {
    callRoutes();
    ipc.handle("career_os_sign_in", () => {
      throw { code: "career_os_unavailable", message: "down" };
    });
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null, settings: { ...SETTINGS, career_os_sign_in: true } })} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with HubRuby" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("HubRuby isn't answering");
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with email" })).toBeInTheDocument();
  });

  it("a stage without email sign-in offers Google only", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps({ tokenSet: false, signedInAs: null, careerOs: null, settings: { ...SETTINGS, email_sign_in: false, career_os_sign_in: false } })} />);
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue with email" })).not.toBeInTheDocument();
    expect(screen.getByText(/With your Google account\./)).toBeInTheDocument();
  });

  it("locked: the HubRuby step asks for the code", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps({ locked: true, careerOs: { connected: false, available: true, required: true } })} />);
    expect(screen.getByRole("heading", { name: /Sign in.*done/ })).toBeInTheDocument();
    expect(screen.getByLabelText("One-time code")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Paste the whole code");
  });

  it("collapses to toolbar and dot, then restores the selected interview and native size", async () => {
    callRoutes();
    mockWindows("main");
    ipc.handle("plugin:window|inner_size", () => ({ width: 1080, height: 780 }));
    ipc.handle("plugin:window|is_decorated", () => true);
    ipc.handle("plugin:window|is_maximized", () => false);
    wrapCallWindow(<CallScreen {...callProps()} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1"));
    await collapseToMedium();
    const compactRuby = await screen.findByRole("button", { name: "Expand full window" });
    const outline = compactRuby.parentElement!.querySelector(".session-compact-outline");
    fireEvent.pointerLeave(compactRuby.closest(".session-window")!);
    const dotRuby = await screen.findByRole("button", { name: "Expand compact toolbar" });
    expect(dotRuby).toBe(compactRuby);
    expect(dotRuby.parentElement!.querySelector(".session-compact-outline")).toBe(outline);
    await userEvent.click(dotRuby);
    await userEvent.click(await screen.findByRole("button", { name: "Expand full window" }));
    await screen.findByRole("button", { name: "Medium window" });
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1");
    expect(ipc.calls.filter((c) => c.cmd === "plugin:window|set_size").map((c) => JSON.parse(JSON.stringify(c.args.value)))).toEqual([
      { Logical: { width: 280, height: 56 } },
      { Physical: { width: 1080, height: 780 } },
    ]);
    expect(ipc.calls.filter((c) => c.cmd === "resize_compact_window").map((c) => c.args)).toEqual([
      { width: 64, height: 44 },
      { width: 280, height: 56 },
    ]);
  });

  it("keeps the full card open on blur, collapses on toolbar leave, and opens on ruby hover", async () => {
    callRoutes();
    mockWindows("main");
    ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 480 }));
    ipc.handle("plugin:window|is_decorated", () => false);
    ipc.handle("plugin:window|is_maximized", () => false);
    ipc.handle("plugin:window|is_focused", () => false);
    ipc.handle("plugin:window|is_minimized", () => false);
    wrapCallWindow(<CallScreen {...callProps()} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1"));
    await act(() => emit("tauri://blur"));
    const surface = screen.getByRole("combobox", { name: "Application" }).closest(".session-window")!;
    fireEvent.pointerLeave(surface);
    await act(() => new Promise(resolve => setTimeout(resolve, 300)));
    expect(surface).toHaveAttribute("data-mini", "full");
    await collapseToMedium();
    fireEvent.pointerLeave(surface);
    const ruby = await screen.findByRole("button", { name: "Expand compact toolbar" });
    ipc.handle("plugin:window|inner_size", () => ({ width: 64, height: 44 }));
    ipc.handle("plugin:window|scale_factor", () => 1);
    ipc.handle("plugin:window|cursor_position", () => ({ x: 32, y: 22 }));
    fireEvent.pointerEnter(ruby);
    await screen.findByRole("button", { name: "Expand full window" });
    const expand = screen.getByRole("button", { name: "Expand setup" });
    await waitFor(() => expect(expand.closest("header")).not.toHaveAttribute("inert"));
    await userEvent.click(expand);
    expect(await screen.findByRole("combobox", { name: "Application" })).toHaveValue("a1");
  });

  it("keeps full-page notices inside the card and hidden throughout compact transitions without remounting data", async () => {
    callRoutes();
    ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [] }));
    mockWindows("main");
    ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
    ipc.handle("plugin:window|is_decorated", () => false);
    ipc.handle("plugin:window|is_maximized", () => false);
    let completeResize!: () => void;
    let resize = new Promise<void>(resolve => { completeResize = resolve; });
    ipc.handle("plugin:window|set_size", () => resize);
    let settingsOpened = false;
    wrapCallWindow(<CallScreen {...callProps({ session: { ...initial, stopped: "unauthorized" }, onSettings: () => { settingsOpened = true; } })} />);
    const resumeNotice = await screen.findByText("Sign-in rejected");
    const authNotice = screen.getByText("Sign-in rejected");
    const surface = resumeNotice.closest(".session-window")!;
    const card = resumeNotice.closest(".call-design")!;
    const notices = resumeNotice.closest(".call-notices")!;
    expect(card).not.toBeNull();
    expect(card.firstElementChild).toHaveClass("call-design-heading");
    expect(resumeNotice).toBeVisible();
    const liveLoads = ipc.apiCalls("GET").filter(call => call.path === "/sessions/live").length;

    await collapseToMedium();
    await waitFor(() => expect(ipc.calls.some(call => call.cmd === "plugin:window|set_size")).toBe(true));
    try {
      expect(surface).toHaveAttribute("data-mini", "full");
      expect(notices).toHaveAttribute("hidden");
      expect(resumeNotice).not.toBeVisible();
      expect(authNotice).not.toBeVisible();
    } finally {
      await act(async () => { completeResize(); await resize; });
    }
    await screen.findByRole("button", { name: "Expand full window" });
    expect(card).toHaveAttribute("hidden");
    fireEvent.pointerLeave(surface);
    await screen.findByRole("button", { name: "Expand compact toolbar" });
    expect(resumeNotice).not.toBeVisible();
    expect(authNotice).not.toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Expand compact toolbar" }));
    resize = new Promise<void>(resolve => { completeResize = resolve; });
    const priorResizes = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
    await userEvent.click(await screen.findByRole("button", { name: "Expand full window" }));
    await waitFor(() => expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(priorResizes + 1));
    try {
      expect(notices).toHaveAttribute("hidden");
      expect(resumeNotice).not.toBeVisible();
    } finally {
      await act(async () => { completeResize(); await resize; });
    }
    await screen.findByRole("button", { name: "Medium window" });
    expect(resumeNotice).toBeVisible();
    expect(authNotice).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1");
    expect(ipc.apiCalls("GET").filter(call => call.path === "/sessions/live")).toHaveLength(liveLoads);
    await userEvent.click(screen.getByRole("button", { name: "Open settings" }));
    expect(settingsOpened).toBe(true);
  });

  it("blocks compact controls during resizing without changing their disabled appearance", async () => {
    callRoutes();
    mockWindows("main");
    ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 480 }));
    ipc.handle("plugin:window|is_decorated", () => false);
    ipc.handle("plugin:window|is_maximized", () => false);
    let finishResize!: () => void;
    const resized = new Promise<void>(resolve => { finishResize = resolve; });
    ipc.handle("resize_compact_window", () => resized);
    wrapCallWindow(<CallScreen {...callProps()} />);
    await collapseToMedium();
    const ruby = await screen.findByRole("button", { name: "Expand full window" });
    const header = ruby.parentElement!;
    const start = within(header).getByRole("button", { name: "Start call" });
    const expand = within(header).getByRole("button", { name: "Expand setup" });
    const settings = within(header).getByRole("button", { name: "Quick settings" });
    fireEvent.pointerLeave(header.closest(".session-window")!);
    await waitFor(() => expect(ipc.calls.some(call => call.cmd === "resize_compact_window")).toBe(true));
    try {
      expect(header).toHaveAttribute("inert");
      expect(start).not.toBeDisabled();
      expect(ruby).not.toBeDisabled();
      expect(expand).not.toBeDisabled();
      expect(settings).not.toBeDisabled();
    } finally {
      await act(async () => { finishResize(); await resized; });
    }
    await screen.findByRole("button", { name: "Expand compact toolbar" });
    await waitFor(() => expect(header).not.toHaveAttribute("inert"));
  });

  it("ready: the next application is chosen without silently selecting a scheduled round", async () => {
    callRoutes();
    wrap(<CallScreen {...callProps()} />);
    expect(await screen.findByRole("option", { name: /Northwind · Backend/, hidden: true })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Application" })).toHaveValue("a1"));
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    const start = ipc.calls.find((c) => c.cmd === "start_session");
    expect(start?.args.request).toMatchObject({ mode: "interview", interview_id: null, job_id: "a1", store_transcript: true });
  });

  it("offers only For interviews and For work and ignores old coding setup preferences", async () => {
    callRoutes();
    ipc.handle("get_settings", () => ({ settings: { ...SETTINGS, coding_mode: "assessment", coding_language: "C" } }));
    wrap(<CallScreen {...callProps()} />);
    await screen.findByRole("option", { name: /Northwind · Backend/, hidden: true });
    const types = screen.getByRole("radiogroup", { name: "Session type" });
    expect(within(types).getAllByRole("radio")).toHaveLength(2);
    expect(within(types).getByRole("radio", { name: "For interviews" })).toBeChecked();
    expect(within(types).getByRole("radio", { name: "For work" })).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Start call" }));
    expect(ipc.calls.find((c) => c.cmd === "start_session")?.args.request).toMatchObject({ mode: "interview", no_call: false, platform: "other", job_id: "a1" });
    expect(ipc.calls.find((c) => c.cmd === "set_coding_prefs")).toBeUndefined();
  });

  it("keeps the live purpose picker to two choices and sends meeting only after an explicit change", async () => {
    callRoutes();
    const live: SessionView = { ...initial, running: true, sessionId: "s1", connection: { state: "live", session_id: "s1", resumed: false } };
    wrap(<CallScreen {...callProps({ session: live })} />);
    const modes = await screen.findByRole("radiogroup", { name: "Mode" });
    expect(within(modes).getAllByRole("radio")).toHaveLength(2);
    expect(within(modes).getByRole("radio", { name: "For interviews" })).toBeChecked();
    expect(ipc.calls.some(call => call.cmd === "set_mode")).toBe(false);
    await userEvent.click(within(modes).getByRole("radio", { name: "For work" }));
    expect(ipc.calls.find(call => call.cmd === "set_mode")?.args).toEqual({ mode: "meeting" });
  });

  it("shown again: an application pulled meanwhile appears in Call for without a restart", async () => {
    callRoutes();
    const apps = [app("a1", "Northwind", "Backend", "interview")];
    ipc.api("GET", /^\/applications$/, () => ({ applications: apps }));
    const { rerender } = wrap(<CallScreen {...callProps({ active: true })} />);
    await screen.findByRole("option", { name: /Northwind · Backend/, hidden: true });
    expect(screen.queryByRole("option", { name: /Lumen/, hidden: true })).toBeNull();
    // Hidden on another tab while a Career-OS pull adds an application, then shown again.
    rerender(<ToastProvider><CallScreen {...callProps({ active: false })} /></ToastProvider>);
    apps.push(app("a2", "Lumen", "Payments"));
    rerender(<ToastProvider><CallScreen {...callProps({ active: true })} /></ToastProvider>);
    expect(await screen.findByRole("option", { name: /Lumen/, hidden: true })).toBeInTheDocument();
  });

  it("live: End call asks how to save it, then ends with a summary", async () => {
    callRoutes();
    ipc.handle("live_status", (): LiveStatus => ({
      running: true, session_id: "s1", stats: null,
      levels: { them_rms: 0.1, me_rms: 0.01, them_speech: 0, me_speech: 0, them_underruns: 0, me_underruns: 0 },
    }));
    const live: SessionView = { ...initial, running: true, sessionId: "s1", connection: { state: "live", session_id: "s1", resumed: false } };
    wrap(<CallScreen {...callProps({ session: live })} />);
    // Keep the call mounted through a real poll, including on a fast test runner.
    await waitFor(() => expect(screen.getByRole("meter", { name: "Them level" })).toHaveAttribute("aria-valuenow", "67"));
    expect(screen.getByRole("meter", { name: "You level" })).toHaveAttribute("aria-valuenow", "33");
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
      ["Call", <CallScreen {...callProps()} />, () => screen.findByRole("combobox", { name: "Application" })],
      ["Call setup", <CallScreen {...callProps({ tokenSet: false, careerOs: null })} />, () => screen.findByRole("heading", { name: "Get ready for your first call" })],
      ["Applications", <Applications />, () => screen.findByRole("heading", { name: "Resume" })],
      ["Resumes", <Resumes />, () => screen.findByText("Northwind v2")],
      ["Work", <Work />, () => screen.findByRole("tab", { name: "Work log" })],
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
