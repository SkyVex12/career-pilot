import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MeetingPreparation } from "../src/MeetingPreparation";
import { ipc } from "./ipc";
import { SettingsScreen } from "../src/SettingsScreen";
import { settingsView } from "./fixtures";

function openPreparation() {
  const details = screen.getByText("Meeting preparation").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  return details;
}

it("shows loading instead of an empty calendar and ignores an earlier failed refresh", async () => {
  let rejectOld!: (reason: string) => void;
  ipc.api("GET", /^\/calendars$/, () => new Promise((_resolve, reject) => { rejectOld = reject; }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ connections: [], items: [] }));
  render(<MeetingPreparation />);
  const disclosure = openPreparation();
  expect(await screen.findByText("Loading calendars…")).toBeInTheDocument();
  expect(screen.queryByText(/Calendar is optional/)).not.toBeInTheDocument();
  disclosure.open = false; fireEvent(disclosure, new Event("toggle"));
  ipc.api("GET", /^\/calendars$/, () => ({ items: [] }));
  openPreparation();
  expect(await screen.findByText(/Calendar is optional/)).toBeVisible();
  await act(async () => { rejectOld("Stale account error"); });
  expect(screen.queryByText("Stale account error")).not.toBeInTheDocument();
});

it("offers reconnect when a retained calendar connection needs new consent", async () => {
  ipc.api("GET", /^\/calendars$/, () => ({ items: [{ provider: "google", available: true, connected: true, status: "reauth_required", calendars: [], selected_calendars: [] }] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ connections: [], items: [] }));
  render(<MeetingPreparation />); openPreparation();
  expect(await screen.findByRole("button", { name: "Reconnect calendar" })).toBeEnabled();
  expect(screen.getByText("Reconnect needed")).toBeVisible();
  expect(screen.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  expect(screen.queryByText("1 calendar connected")).not.toBeInTheDocument();
});

it("closes a successful disconnect even if refresh fails and removes disconnected calendar data", async () => {
  ipc.api("GET", /^\/calendars$/, () => ({ items: [{ provider: "google", available: true, connected: true, status: "connected", calendars: [{ id: "cal", name: "Private calendar" }], selected_calendars: ["cal"] }] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ connections: [], items: [] }));
  ipc.api("DELETE", /^\/calendars\/google$/, () => {
    ipc.api("GET", /^\/calendars$/, () => { throw "Refresh offline"; });
    return { provider_consent_removal_required: false };
  });
  render(<MeetingPreparation />); openPreparation();
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  expect(await screen.findByText("Refresh offline")).toBeVisible();
  expect(screen.queryByText("Private calendar")).not.toBeInTheDocument();
  expect(screen.getByText("Not connected")).toBeVisible();
  expect(ipc.apiCalls("DELETE")).toHaveLength(1);
});

it("clears calendar settings immediately when the active profile changes", async () => {
  ipc.handle("careeros_status", () => ({ connected: false }));
  ipc.api("GET", /^\/calendars$/, () => ({ items: [{ provider: "google", available: true, connected: true, status: "connected", calendars: [{ id: "cal", name: "First profile calendar" }], selected_calendars: ["cal"] }] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ connections: [], items: [] }));
  const props = { view: settingsView(), running: false, onSaved: () => {}, update: { state: "none" } as const, check: { busy: false, error: null, start: () => {} }, section: "account" as const };
  const view = render(<SettingsScreen {...props} profileScope="account/first" />); openPreparation();
  expect(await screen.findByText("First profile calendar")).toBeVisible();
  ipc.api("GET", /^\/calendars$/, () => ({ items: [] }));
  view.rerender(<SettingsScreen {...props} profileScope="account/second" />);
  expect(screen.queryByText("First profile calendar")).not.toBeInTheDocument();
  expect(screen.getByText("Meeting preparation").closest("details")).not.toHaveAttribute("open");
  openPreparation();
  expect(await screen.findByText(/Calendar is optional/)).toBeVisible();
});
it("loads calendar context only when opened and applies a confirmed match without starting a call", async () => {
  const onChoose = vi.fn();
  ipc.api("GET", /^\/calendars$/, () => ({items:[{provider:"google",available:true,connected:true,status:"connected",calendars:[{id:"cal",name:"Interviews"}],selected_calendars:["cal"]}]}));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({connections:[{connection_id:"connection",provider:"google"}],items:[{meeting:{account_id:"connection",calendar_id:"cal",instance_id:"event",title:"Acme interview",start:"2026-11-20T15:00:00Z",end:"2026-11-20T16:00:00Z"},candidates:[{application_id:"app",company:"Acme",job_title:"Engineer"}],selected_application_id:null,requires_confirmation:true,overlaps:true,previous:[{notes:"Prior discussion",unfinished_actions:["Review the design"]}]}]}));
  render(<MeetingPreparation onChoose={onChoose}/>);
  expect(ipc.apiCalls()).toHaveLength(0);
  const disclosure = screen.getByText("Meeting preparation").closest("details")!;
  disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
  expect(await screen.findByText("Acme interview")).toBeVisible();
  expect(onChoose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("combobox",{name:"Meeting application"}));
  fireEvent.click(screen.getByRole("option",{name:"Acme · Engineer"}));
  fireEvent.click(screen.getByRole("button",{name:"Use for call setup"}));
  expect(onChoose).toHaveBeenCalledWith("Acme interview","app");
  expect(ipc.calls.some(c => ["start_session","open_session"].includes(c.cmd))).toBe(false);
});
it("opens provider consent only on explicit connect", async () => {
  ipc.api("GET", /^\/calendars$/, () => ({items:[{provider:"google",available:true,connected:false,status:"disconnected",calendars:[],selected_calendars:[]}]}));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({connections:[],items:[]}));
  ipc.api("POST", /^\/calendars\/google\/connect$/, () => ({authorization_url:"https://accounts.google.com/o/oauth2/v2/auth?state=opaque"}));
  render(<MeetingPreparation/>);
  const disclosure = screen.getByText("Meeting preparation").closest("details")!;
  disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
  const connect = await screen.findByRole("button",{name:"Connect read-only calendar"});
  expect(ipc.calls.some(c => c.cmd === "open_calendar_authorization")).toBe(false);
  fireEvent.click(connect);
  await waitFor(() => expect(ipc.calls.find(c => c.cmd === "open_calendar_authorization")?.args.url).toBe("https://accounts.google.com/o/oauth2/v2/auth?state=opaque"));
});


it.each([false, true])("preserves saved application, call and action edits when refresh fails: %s", async (failRefresh) => {
  const context = { application_id: null as string | null, session_id: null as string | null, unfinished_actions: ["Already completed"] };
  ipc.api("GET", /^\/calendars$/, () => ({ items: [] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({
    connections: [{ connection_id: "connection", provider: "google" }],
    applications: [{ application_id: "unmatched", company: "Other company", job_title: "Designer" }],
    saved_sessions: [{ id: "prior-call", title: "Design review", started_at: "2026-11-18T15:00:00Z" }],
    items: [{ meeting: { account_id: "connection", calendar_id: "cal", instance_id: "event", title: "Weekly discussion", start: "2026-11-20T15:00:00Z", end: "2026-11-20T16:00:00Z", series_id: "series" }, candidates: [], selected_application_id: context.application_id, linked_session_id: context.session_id, unfinished_actions: context.unfinished_actions, previous: [], overlaps: false, requires_confirmation: false }],
  }));
  ipc.api("PUT", /^\/calendars\/google\/context$/, (_path, body) => {
    Object.assign(context, body);
    if (failRefresh) ipc.api("GET", /^\/meeting-preparation$/, () => { throw "Refresh failed after saving"; });
    return { saved: true };
  });
  render(<MeetingPreparation />);
  const disclosure = screen.getByText("Meeting preparation").closest("details")!;
  disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
  expect(await screen.findByText("Weekly discussion")).toBeVisible();
  fireEvent.click(screen.getByRole("combobox", { name: "Meeting application" }));
  fireEvent.click(screen.getByRole("option", { name: "Other company · Designer" }));
  fireEvent.click(screen.getByRole("combobox", { name: "Related saved call" }));
  fireEvent.click(screen.getByRole("option", { name: /Design review/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "Unfinished actions" }), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save meeting context" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Meeting context saved.");
  expect(ipc.apiCalls("PUT")[0].body).toEqual({ calendar_id: "cal", instance_id: "event", application_id: "unmatched", session_id: "prior-call", unfinished_actions: [] });
  expect(screen.getByRole("combobox", { name: "Related saved call" })).toHaveTextContent("Design review");
  expect(screen.getByRole("textbox", { name: "Unfinished actions" })).toHaveValue("");
  expect(screen.getByRole("combobox", { name: "Meeting application" })).toHaveTextContent("Other company · Designer");
  if (failRefresh) expect(screen.getByText("Refresh failed after saving")).toBeVisible();
  expect(ipc.calls.some(c => ["start_session", "open_session"].includes(c.cmd))).toBe(false);
});

it("does not open a previous profile’s delayed calendar authorization", async () => {
  let authorize!: (value: unknown) => void;
  ipc.api("GET", /^\/calendars$/, () => ({ items: [{ provider: "google", available: true, connected: false, status: "disconnected", calendars: [], selected_calendars: [] }] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({ connections: [], items: [] }));
  ipc.api("POST", /^\/calendars\/google\/connect$/, () => new Promise(resolve => { authorize = resolve; }));
  const view = render(<MeetingPreparation />); openPreparation();
  fireEvent.click(await screen.findByRole("button", { name: "Connect read-only calendar" }));
  await waitFor(() => expect(authorize).toBeDefined());
  view.unmount();
  const reads = ipc.apiCalls("GET").length;
  await act(async () => { authorize({ authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=previous-profile" }); });
  expect(ipc.calls.some(c => c.cmd === "open_calendar_authorization")).toBe(false);
  expect(ipc.apiCalls("GET")).toHaveLength(reads);
});
