import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { BackgroundSuggestion, type CallSuggestion } from "../src/BackgroundSuggestion";
import { ipc } from "./ipc";
it("accepts only the local suggestion without creating a call or calling an API",async()=>{
  ipc.handle("background_suggestion",()=>({id:"42:Zoom",platform:"Zoom",kind:"start"}));
  ipc.handle("background_respond",()=>null);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Call detected")).toBeVisible();
  expect(ipc.apiCalls()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button",{name:"Start Copilot"}));
  await waitFor(()=>expect(ipc.calls.some(c=>c.cmd==="background_respond" && c.args.accept===true)).toBe(true));
  expect(ipc.calls.some(c=>c.cmd==="start_session")).toBe(false);
  expect(ipc.apiCalls()).toHaveLength(0);
});
it("dismisses a call-end offer without stopping the active session",async()=>{
  ipc.handle("background_suggestion",()=>({id:"42:Zoom",platform:"Call",kind:"end"}));
  ipc.handle("background_respond",()=>null);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Call may have ended")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"Not now"}));
  await waitFor(()=>expect(ipc.calls.some(c=>c.cmd==="background_respond" && c.args.accept===false)).toBe(true));
  expect(ipc.calls.some(c=>c.cmd==="end_session")).toBe(false);
});

it("shows an incoming Hushed call and opens only the existing local acceptance flow",async()=>{
  ipc.handle("background_suggestion",()=>({id:"call:7:emulator-5554:42:Hushed",platform:"Hushed",kind:"start",phase:"incoming"}));
  ipc.handle("background_respond",()=>null);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Incoming Hushed call")).toBeVisible();
  expect(screen.getByText("Start Copilot for live assistance when you answer.")).toBeVisible();
  expect(ipc.apiCalls()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button",{name:"Start Copilot"}));
  await waitFor(()=>expect(ipc.calls.some(c=>c.cmd==="background_respond" && c.args.id==="call:7:emulator-5554:42:Hushed" && c.args.accept===true)).toBe(true));
  expect(ipc.calls.every(c=>["background_suggestion","background_respond"].includes(c.cmd))).toBe(true);
  expect(ipc.apiCalls()).toHaveLength(0);
});

it("updates ringing to active in the same suggestion and clears a retired call",async()=>{
  let current:CallSuggestion|null={id:"call:7:emulator-5554:42:Hushed",platform:"Hushed",kind:"start",phase:"incoming"};
  ipc.handle("background_suggestion",()=>current);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Incoming Hushed call")).toBeVisible();
  current={...current,phase:"active"};
  await act(()=>emit("copilot://call-suggestion",current));
  expect(await screen.findByText("Call detected")).toBeVisible();
  expect(screen.queryByText("Incoming Hushed call")).not.toBeInTheDocument();
  expect(screen.getAllByRole("button",{name:"Start Copilot"})).toHaveLength(1);
  current=null;
  await act(()=>emit("copilot://call-suggestion",null));
  await waitFor(()=>expect(screen.queryByRole("button",{name:"Start Copilot"})).not.toBeInTheDocument());
  expect(ipc.calls.some(c=>c.cmd==="background_respond")).toBe(false);
  expect(ipc.apiCalls()).toHaveLength(0);
});

it("dismisses an incoming Hushed suggestion without answering or starting capture",async()=>{
  ipc.handle("background_suggestion",()=>({id:"call:7:Hushed",platform:"Hushed",kind:"start",phase:"incoming"}));
  ipc.handle("background_respond",()=>null);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Incoming Hushed call")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"Not now"}));
  await waitFor(()=>expect(ipc.calls.some(c=>c.cmd==="background_respond" && c.args.accept===false)).toBe(true));
  expect(ipc.calls.every(c=>["background_suggestion","background_respond"].includes(c.cmd))).toBe(true);
  expect(ipc.apiCalls()).toHaveLength(0);
});
