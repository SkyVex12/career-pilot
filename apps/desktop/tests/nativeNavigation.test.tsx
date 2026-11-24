import { emit } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, it } from "vitest";
import App from "../src/App";
import { app, detail, settingsView } from "./fixtures";
import { ipc } from "./ipc";

// Load the real lazy screen before interaction deadlines, outside cold CI transforms.
beforeAll(async () => {
  await Promise.all([import("../src/SettingsScreen"), import("../src/Debug")]);
});

async function setup(applications: ReturnType<typeof app>[] = []) {
  mockWindows("main");
  ipc.handle("get_settings", () => settingsView());
  ipc.handle("snapshot", () => [[], false, 0]);
  ipc.handle("update_status", () => ({ state: "none" }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|cursor_position", () => ({ x: 1000, y: 1000 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("plugin:window|is_minimized", () => false);
  ipc.handle("plugin:window|is_resizable", () => true);
  ipc.handle("set_compact_window_shape", () => true);
  ipc.api("GET", /^\/profiles$/, () => ({ items: [{ id: "account", name: "Dana", me: true, career_os_user_id: null }] }));
  ipc.api("GET", /^\/career-os\/connection$/, () => ({ connected: true, available: true }));
  ipc.api("GET", /^\/applications$/, () => ({ applications }));
  ipc.api("GET", /^\/applications\/[^/]+$/, path => detail(applications.find(a => path.endsWith(a.id))!));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [] }));
  // Settle settings and profile bootstrap before polling the interactive controls.
  await act(async () => { render(<App />); });
}

async function compact(mode: "toolbar" | "dot") {
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Profile" })).toHaveTextContent("DA"));
  await userEvent.click(screen.getByRole("button", { name: "Medium window" }));
  const ruby = await screen.findByRole("button", { name: "Keep medium window open" });
  await waitFor(() => expect(ruby.closest("header")).not.toHaveAttribute("inert"));
  if (mode === "dot") {
    fireEvent.pointerLeave(ruby.closest(".session-window")!);
    await screen.findByRole("button", { name: "Expand compact toolbar" });
    await waitFor(() => expect(ruby.closest("header")).not.toHaveAttribute("inert"));
  }
  return ruby.closest(".session-window")!;
}

function holdRestore() {
  let complete!: () => void;
  let started = false;
  ipc.handle("plugin:window|set_size", () => {
    started = true;
    return new Promise<void>(resolve => { complete = resolve; });
  });
  return {
    wait: () => waitFor(() => expect(started).toBe(true)),
    finish: () => act(async () => complete()),
  };
}

it.each(["toolbar", "dot"] as const)("restores %s bounds before opening native Settings", async mode => {
  await setup();
  const surface = await compact(mode);
  const restore = holdRestore();
  await act(() => emit("copilot://open-settings"));
  await restore.wait();
  expect(surface).toHaveAttribute("data-mini", mode);
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  await restore.finish();
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
  expect(ipc.calls.filter(call => call.cmd === "set_compact_window_shape").slice(-1)[0]?.args).toEqual({ shape: "full" });
  const bounds = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").slice(-1)[0]?.args.value;
  expect(JSON.parse(JSON.stringify(bounds))).toEqual({ Logical: { width: 648, height: 520 } });
  expect(ipc.calls.some(call => ["start_session", "open_session", "end_session"].includes(call.cmd))).toBe(false);
});

it("restores compact bounds for the idle overlay's return-to-call event", async () => {
  await setup();
  const surface = await compact("dot");
  await act(() => emit("copilot://open-call"));
  await waitFor(() => expect(surface).toHaveAttribute("data-mini", "full"));
  expect(screen.getByRole("button", { name: "Medium window" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(ipc.calls.some(call => ["start_session", "open_session", "end_session"].includes(call.cmd))).toBe(false);
});

it("keeps compact controls reachable when expansion fails and allows a later retry", async () => {
  await setup();
  const surface = await compact("toolbar");
  ipc.handle("set_compact_window_shape", ({ shape }) => {
    if (shape === "full") throw "Could not restore the window";
    return true;
  });
  await act(() => emit("copilot://open-settings"));
  await waitFor(() => expect(document.documentElement).not.toHaveAttribute("data-window-transition"));
  expect(surface).toHaveAttribute("data-mini", "toolbar");
  expect(screen.getByRole("button", { name: "Keep medium window open" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  ipc.handle("set_compact_window_shape", () => true);
  await act(() => emit("copilot://open-settings"));
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
});

it("uses the latest native destination during a single pending expansion", async () => {
  await setup();
  const surface = await compact("toolbar");
  const before = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
  const restore = holdRestore();
  await act(() => emit("copilot://open-settings"));
  await restore.wait();
  await act(() => emit("copilot://open-call"));
  await restore.finish();
  await waitFor(() => expect(surface).toHaveAttribute("data-mini", "full"));
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(before + 1);
});

const toggleDebug = () => fireEvent.keyDown(window, { key: "D", ctrlKey: true, shiftKey: true });

it.each(["toolbar", "dot"] as const)("restores %s bounds before the debug shortcut opens its page, then toggles back to Call", async mode => {
  await setup();
  const surface = await compact(mode);
  const before = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
  const restore = holdRestore();
  toggleDebug();
  await restore.wait();
  expect(surface).toHaveAttribute("data-mini", mode);
  expect(screen.queryByRole("heading", { name: "Debug" })).not.toBeInTheDocument();
  await restore.finish();
  expect(await screen.findByRole("heading", { name: "Debug" })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
  const bounds = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").slice(-1)[0]?.args.value;
  expect(JSON.parse(JSON.stringify(bounds))).toEqual({ Logical: { width: 648, height: 520 } });
  toggleDebug();
  expect(await screen.findByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Debug" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Debug" })).not.toBeInTheDocument();
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(before + 1);
});

it.each(["settings-first", "debug-first"] as const)("shares one pending restoration between native Settings and the debug shortcut (%s)", async order => {
  await setup();
  const surface = await compact("toolbar");
  const before = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
  const restore = holdRestore();
  if (order === "settings-first") await act(() => emit("copilot://open-settings"));
  else toggleDebug();
  await restore.wait();
  if (order === "settings-first") toggleDebug();
  else await act(() => emit("copilot://open-settings"));
  expect(screen.queryByRole("heading", { name: "Debug" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  await restore.finish();
  const destination = order === "settings-first" ? "Debug" : "Settings";
  expect(await screen.findByRole("heading", { name: destination })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(before + 1);
});

it("toggles a pending debug destination back to Call without starting another restoration", async () => {
  await setup();
  const surface = await compact("dot");
  const before = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
  const restore = holdRestore();
  toggleDebug();
  await restore.wait();
  toggleDebug();
  expect(surface).toHaveAttribute("data-mini", "dot");
  await restore.finish();
  expect(await screen.findByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
  expect(screen.queryByRole("heading", { name: "Debug" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Debug" })).not.toBeInTheDocument();
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(before + 1);
});


it("adds an application directly from Call and selects it for starting", async () => {
  const created = detail(app("a3", "New company", "Engineer"));
  let applications = [app("a1", "Northwind", "Backend")];
  await setup(applications);
  ipc.api("GET", /^\/applications$/, () => ({ applications }));
  ipc.api("POST", /^\/applications$/, () => { applications = [...applications, created]; return created; });
  ipc.api("GET", /^\/applications\/a3$/, () => created);
  const application = await screen.findByRole("combobox", { name: "Application" });
  await userEvent.click(within(application.closest(".call-application-row")!).getByRole("button", { name: "Add" }));
  const dialog = screen.getByRole("dialog", { name: "Add application" });
  await userEvent.type(within(dialog).getByRole("textbox", { name: "Company" }), "New company");
  await userEvent.type(within(dialog).getByRole("textbox", { name: "Role" }), "Engineer");
  await userEvent.type(within(dialog).getByRole("textbox", { name: /Job description/ }), "Build reliable systems.");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add application" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add application" })).not.toBeInTheDocument());
  expect(application).toHaveValue("a3");
  expect(screen.getByRole("button", { name: "Call" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("button", { name: "Start call" })).toBeVisible();
});

it("places Calendar immediately after Call and loads calendar preparation", async () => {
  ipc.api("GET", /^\/calendars$/, () => ({ items: [] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ items: [], connections: [] }));
  await setup();
  const call = screen.getByRole("button", { name: "Call" });
  const calendar = screen.getByRole("button", { name: "Calendar" });
  expect(call.compareDocumentPosition(calendar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await userEvent.click(calendar);
  expect(await screen.findByRole("heading", { name: "Calendar" })).toBeVisible();
  expect(await screen.findByText("Calendar is optional. You can start a call whenever you’re ready.")).toBeVisible();
});

it("opens the shared resume editor within Interview prep from an unpaired general call", async () => {
  await setup();
  await userEvent.click(await screen.findByRole("button", { name: "Add resume" }));
  const dialog = await screen.findByRole("dialog", { name: "Add your base resume" });
  expect(within(dialog).getByLabelText("Resume text")).toBeVisible();
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("heading", { name: "Interview prep" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Default resume" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Resumes" })).not.toBeInTheDocument();
});

it("opens Ask inside Work and practice inside Interview prep without adding main tabs", async () => {
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({ requests: [] }));
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  ipc.api("GET", /^\/ask\/context-options/, () => ({ options: [] }));
  ipc.api("GET", /^\/practice\/sessions$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/practice\/context/, () => ({ sources: [], notice: "Current profile" }));
  await setup();
  const main = screen.getByRole("navigation", { name: "Main" });
  await act(async () => { window.dispatchEvent(new CustomEvent("copilot:open-ask", { detail: { kind: "general" } })); });
  expect(await screen.findByLabelText("Ask a question")).toBeVisible();
  expect(within(main).getByRole("button", { name: "Work context" })).toHaveAttribute("aria-current", "page");
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "Keep this thought" } });
  fireEvent.click(screen.getByRole("button", { name: "Back to work context" }));
  fireEvent.click(screen.getByRole("button", { name: /^Ask a question$/ }));
  expect(screen.getByLabelText("Ask a question")).toHaveValue("Keep this thought");
  await act(async () => { window.dispatchEvent(new CustomEvent("copilot:open-practice", { detail: { topic: "A saved interview question" } })); });
  expect(await screen.findByLabelText("Role or topic")).toHaveValue("A saved interview question");
  expect(within(main).getByRole("button", { name: "Interview prep" })).toHaveAttribute("aria-current", "page");
  expect(within(main).queryByRole("button", { name: /^Ask$/ })).toBeNull();
  expect(within(main).queryByRole("button", { name: /^Practice$/ })).toBeNull();
  expect(ipc.calls.some(call => ["start_session", "open_session"].includes(call.cmd))).toBe(false);
});
