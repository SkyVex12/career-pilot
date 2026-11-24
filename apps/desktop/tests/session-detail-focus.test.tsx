import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { SessionDetail } from "../src/SessionDetail";
import { ipc } from "./ipc";
const session = { id:"s1", status:"ready", mode:"interview", store_transcript:true, questions:0, notes:"", outputs:{summary:"Saved summary"} };
const replay = { session_id:"s1", stored:true, started_at:null, questions:[], segments:[
  {channel:"them",seg_id:1,start_ms:1000,end_ms:1500,text:"Opening question"},
  {channel:"them",seg_id:2,start_ms:3000,end_ms:4500,text:"Supporting evidence"},
  {channel:"me",seg_id:2,start_ms:5000,end_ms:6000,text:"My answer"},
] };
it("downloads replay only when a transcript or replay tab is opened and then reuses it", async () => {
  ipc.api("GET", /^\/sessions\/s1$/, () => session);
  ipc.api("GET", /^\/sessions\/s1\/replay$/, () => replay);
  render(<SessionDetail sessionId="s1" />);
  await screen.findByText("Saved summary");
  expect(ipc.apiCalls("GET").filter(call => call.path.endsWith("/replay"))).toHaveLength(0);
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Transcript" }), { button: 0, ctrlKey: false });
  fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
  await screen.findByText("Supporting evidence");
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Replay" }), { button: 0, ctrlKey: false });
  fireEvent.click(screen.getByRole("tab", { name: "Replay" }));
  expect(ipc.apiCalls("GET").filter(call => call.path.endsWith("/replay"))).toHaveLength(1);
});
it("polls only the small status while processing and fetches finished content once", async () => {
  vi.useFakeTimers();
  try {
    let ready = false;
    ipc.api("GET", /^\/sessions\/s1$/, () => ready ? session : { ...session, status: "processing", outputs: {} });
    ipc.api("GET", /^\/sessions\/s1\/status$/, () => ({ id: "s1", status: ready ? "ready" : "processing", revision: 1 }));
    render(<SessionDetail sessionId="s1" />);
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(ipc.apiCalls("GET").filter(call => call.path === "/sessions/s1")).toHaveLength(1);
    expect(ipc.apiCalls("GET").filter(call => call.path === "/sessions/s1/status")).toHaveLength(3);
    ready = true;
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(screen.getByText("Saved summary")).toBeVisible();
    expect(ipc.apiCalls("GET").filter(call => call.path === "/sessions/s1")).toHaveLength(2);
    expect(ipc.apiCalls("GET").some(call => call.path.endsWith("/replay"))).toBe(false);
  } finally { vi.useRealTimers(); }
});
it("opens Transcript and scrolls to the timestamp only after replay arrives",async()=>{
  const scroll=vi.spyOn(Element.prototype,"scrollIntoView");
  try {
    let finish!:(value:unknown)=>void;
    ipc.api("GET",/^\/sessions\/s1$/,()=>session);
    ipc.api("GET",/^\/sessions\/s1\/replay$/,()=>new Promise(resolve=>{finish=resolve;}));
    render(<SessionDetail sessionId="s1" transcriptFocus={{sessionId:"s1",startMs:3500}}/>);
    expect(await screen.findByRole("tab",{name:"Transcript"})).toHaveAttribute("aria-selected","true");
    expect(scroll).not.toHaveBeenCalled();
    await act(async()=>{finish(replay);});
    const text=await screen.findByText("Supporting evidence");
    expect(text.tagName).toBe("MARK");
    expect(text.closest("li")).toHaveAttribute("aria-current","location");
    await waitFor(()=>expect(scroll).toHaveBeenCalledWith({block:"center",behavior:"auto"}));
    expect(screen.getByText("Opening question").closest("li")).not.toHaveAttribute("aria-current");
  } finally {scroll.mockRestore();}
});
it("ignores another session's target and focuses exact channel/segment when supplied",async()=>{
  ipc.api("GET",/^\/sessions\/s1$/,()=>session);
  ipc.api("GET",/^\/sessions\/s1\/replay$/,()=>replay);
  const view=render(<SessionDetail sessionId="s1" transcriptFocus={{sessionId:"other",startMs:3500}}/>);
  expect(await screen.findByText("Saved summary")).toBeVisible();
  expect(screen.getByRole("tab",{name:"Transcript"})).toHaveAttribute("aria-selected","false");
  view.rerender(<SessionDetail sessionId="s1" transcriptFocus={{sessionId:"s1",channel:"me",segId:2}}/>);
  expect((await screen.findByText("My answer")).tagName).toBe("MARK");
  expect(screen.getByText("Supporting evidence").closest("li")).not.toHaveAttribute("aria-current");
  view.rerender(<SessionDetail sessionId="s1" transcriptFocus={{sessionId:"s1",startMs:999999}}/>);
  expect(await screen.findByText("The referenced transcript moment is unavailable.")).toBeVisible();
  expect(screen.getByText("My answer").tagName).not.toBe("MARK");
});
