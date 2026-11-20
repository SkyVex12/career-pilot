import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MeetingPreparation } from "../src/MeetingPreparation";
import { ipc } from "./ipc";
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


it("corrects unmatched applications, links saved calls, and saves completed actions as empty", async () => {
  const context = { application_id: null as string | null, session_id: null as string | null, unfinished_actions: ["Already completed"] };
  ipc.api("GET", /^\/calendars$/, () => ({ items: [] }));
  ipc.api("GET", /^\/meeting-preparation$/, () => ({
    connections: [{ connection_id: "connection", provider: "google" }],
    applications: [{ application_id: "unmatched", company: "Other company", job_title: "Designer" }],
    saved_sessions: [{ id: "prior-call", title: "Design review", started_at: "2026-11-18T15:00:00Z" }],
    items: [{ meeting: { account_id: "connection", calendar_id: "cal", instance_id: "event", title: "Weekly discussion", start: "2026-11-20T15:00:00Z", end: "2026-11-20T16:00:00Z", series_id: "series" }, candidates: [], selected_application_id: context.application_id, linked_session_id: context.session_id, unfinished_actions: context.unfinished_actions, previous: [], overlaps: false, requires_confirmation: false }],
  }));
  ipc.api("PUT", /^\/calendars\/google\/context$/, (_path, body) => {
    Object.assign(context, body); return { saved: true };
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
  expect(ipc.calls.some(c => ["start_session", "open_session"].includes(c.cmd))).toBe(false);
});
