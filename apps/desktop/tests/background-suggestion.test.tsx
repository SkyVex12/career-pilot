import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { BackgroundSuggestion } from "../src/BackgroundSuggestion";
import { ipc } from "./ipc";
it("accepts only the local suggestion without creating a call or calling an API",async()=>{
  ipc.handle("background_suggestion",()=>({id:"42:Zoom",platform:"Zoom",kind:"start"}));
  ipc.handle("background_respond",()=>null);
  render(<BackgroundSuggestion/>);
  expect(await screen.findByText("Call detected")).toBeVisible();
  expect(ipc.apiCalls()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button",{name:"Start CoRuby"}));
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
