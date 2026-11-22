import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { PersonalAI } from "../src/PersonalAI";
import { ipc } from "./ipc";

const brief = { title: "Progress", prepared_at: "2026-11-19T12:00:00Z", items: [{ id:"i1", text:"Shipped queue", provenance:"user_report", source_ids:[] }], sources:[], coverage_gaps:["No tracker access"] };
const request = () => ({ id:"r1", scope:{ title:"Standup", questions:["What shipped?"], date_from:"2026-11-18", date_to:"2026-11-19" }, request_revision:1, expected_brief_format:{ type:"object", required:["items","sources"] }, active_version_id:"v1", active_brief:{ id:"v1", brief, origin:"paste" } });

it("includes scope and schema in the fallback and preserves retry identity", async () => {
  const r = request(); const keys: string[] = [];
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({ requests:[r] }));
  ipc.api("GET", /^\/personal-ai\/requests\/r1$/, () => r);
  ipc.api("POST", /^\/personal-ai\/requests\/r1\/publish$/, (_p,b) => { keys.push((b as {idempotency_key:string}).idempotency_key); throw new Error("Offline"); });
  render(<PersonalAI />);
  fireEvent.click(await screen.findByRole("button", { name:"View" }));
  fireEvent.click(await screen.findByRole("button", { name:"Show AI request" }));
  const handoff = screen.getByLabelText("Request to run in your AI");
  expect((handoff as HTMLTextAreaElement).value).toContain("What shipped?");
  expect((handoff as HTMLTextAreaElement).value).toContain("expected_brief_format");
  fireEvent.click(screen.getByText("Paste a brief from your AI"));
  fireEvent.change(screen.getByLabelText("Brief JSON"), { target:{value:JSON.stringify(brief)} });
  fireEvent.click(screen.getByRole("button", {name:"Import pasted brief"}));
  await waitFor(() => expect(keys).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole("button", {name:"Import pasted brief"})).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", {name:"Import pasted brief"}));
  await waitFor(() => expect(keys).toHaveLength(2)); expect(keys[0]).toBe(keys[1]);
  expect(screen.getByLabelText("Brief JSON")).toHaveValue(JSON.stringify(brief));
});

it("corrects and removes individual items using the current version and deletes the request", async () => {
  let r = request(); let removed = false; const patches: unknown[] = [];
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({ requests:removed ? [] : [r] }));
  ipc.api("GET", /^\/personal-ai\/requests\/r1$/, () => r);
  ipc.api("PATCH", /^\/personal-ai\/requests\/r1\/items\/i1$/, (_p,b) => {
    const body = b as {text:string|null}; patches.push(b);
    r = { ...r, active_version_id:`v${patches.length+1}`, active_brief:{ ...r.active_brief, brief:{ ...brief, items:body.text === null ? [] : [{...brief.items[0],text:body.text}] } } }; return {};
  });
  ipc.api("DELETE", /^\/personal-ai\/requests\/r1$/, () => { removed=true; return {}; });
  render(<PersonalAI />); fireEvent.click(await screen.findByRole("button", {name:"View"}));
  fireEvent.change(await screen.findByLabelText("Brief item"), {target:{value:"Tested queue"}});
  fireEvent.click(screen.getByRole("button", {name:"Save correction"}));
  await waitFor(() => expect(patches).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole("button", {name:"Remove item"})).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", {name:"Remove item"}));
  await waitFor(() => expect(screen.queryByLabelText("Brief item")).not.toBeInTheDocument());
  expect(patches).toEqual([{expected_version_id:"v1",text:"Tested queue"},{expected_version_id:"v2",text:null}]);
  await waitFor(() => expect(screen.getByRole("button", {name:"Remove"})).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", {name:"Remove"}));
  await waitFor(() => expect(screen.queryByRole("button", {name:"Show AI request"})).not.toBeInTheDocument());
});


it("keeps the usable brief visible when preparing a refresh fails", async () => {
  const r=request();
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({requests:[r]}));
  ipc.api("GET", /^\/personal-ai\/requests\/r1$/, () => r);
  ipc.api("POST", /^\/personal-ai\/requests\/r1\/refresh$/, () => { throw new Error("Host unavailable"); });
  render(<PersonalAI />); fireEvent.click(await screen.findByRole("button", {name:"View"}));
  expect(await screen.findByLabelText("Brief item")).toHaveValue("Shipped queue");
  await waitFor(() => expect(screen.getByRole("button", {name:"Prepare refresh request"})).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", {name:"Prepare refresh request"}));
  expect(await screen.findByText(/Host unavailable/)).toBeVisible();
  expect(screen.getByLabelText("Brief item")).toHaveValue("Shipped queue");
  expect(screen.getByText(/Pasted from your AI/)).toBeVisible();
});


it("disconnects only the selected AI grant and preserves existing briefs", async () => {
  const r = request(); let connected = true;
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({ requests:[r] }));
  ipc.api("GET", /^\/personal-ai\/oauth\/connections$/, () => ({ connections:connected ? [{id:"grant1",client_id:"Test AI",scope:"context:read",expires_at:1790000000}] : [] }));
  ipc.api("DELETE", /^\/personal-ai\/oauth\/connections\/grant1$/, () => { connected=false; return {}; });
  render(<PersonalAI />);
  fireEvent.click(screen.getByText("Connected AI clients"));
  fireEvent.click(await screen.findByRole("button", {name:"Disconnect Test AI"}));
  await waitFor(() => expect(screen.queryByRole("button", {name:"Disconnect Test AI"})).not.toBeInTheDocument());
  expect(screen.getByText("No active OAuth connections.")).toBeVisible();
  expect(screen.getByRole("button", {name:"View"})).toBeVisible();
});


it("ignores an initial grant list that finishes after disconnect", async () => {
  let finish!: (value: unknown) => void; let reads = 0; let connected = true;
  const grant = {id:"g1",client_id:"Delayed AI",scope:"context:read",expires_at:1790000000};
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({requests:[]}));
  ipc.api("GET", /^\/personal-ai\/oauth\/connections$/, () => ++reads === 1 ? new Promise(resolve => {finish=resolve;}) : {connections:connected ? [grant] : []});
  ipc.api("DELETE", /^\/personal-ai\/oauth\/connections\/g1$/, () => {connected=false; return {};});
  render(<PersonalAI />); fireEvent.click(screen.getByText("Connected AI clients"));
  fireEvent.click(screen.getByRole("button", {name:"Refresh AI connections"}));
  const disconnect = await screen.findByRole("button", {name:"Disconnect Delayed AI"});
  await waitFor(() => expect(disconnect).not.toBeDisabled()); fireEvent.click(disconnect);
  await waitFor(() => expect(screen.queryByRole("button", {name:"Disconnect Delayed AI"})).not.toBeInTheDocument());
  finish({connections:[grant]});
  await waitFor(() => expect(screen.getByText("No active OAuth connections.")).toBeVisible());
});

it("closing a brief invalidates an in-flight detail refresh", async () => {
  const r = request(); let finish!: (value: unknown) => void; let reads = 0;
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({requests:[r]}));
  ipc.api("GET", /^\/personal-ai\/oauth\/connections$/, () => ({connections:[]}));
  ipc.api("GET", /^\/personal-ai\/requests\/r1$/, () => ++reads === 1 ? r : new Promise(resolve => {finish=resolve;}));
  render(<PersonalAI />); fireEvent.click(await screen.findByRole("button", {name:"View"}));
  await screen.findByRole("button", {name:"Close brief"});
  await waitFor(() => expect(screen.getByRole("button", {name:"View"})).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", {name:"View"}));
  fireEvent.click(screen.getByRole("button", {name:"Close brief"}));
  finish(r);
  await waitFor(() => expect(screen.getByRole("button", {name:"View"})).not.toBeDisabled());
  expect(screen.queryByRole("button", {name:"Close brief"})).not.toBeInTheDocument();
});

it("does not report an unavailable connection service as an empty connection list", async () => {
  ipc.api("GET", /^\/personal-ai\/requests$/, () => ({ requests: [] }));
  ipc.api("GET", /^\/personal-ai\/oauth\/connections$/, () => { throw new Error("Service unavailable"); });
  render(<PersonalAI />);
  fireEvent.click(screen.getByText("Connected AI clients"));
  expect(await screen.findByText("Connection status is unavailable. Try refreshing.")).toBeVisible();
  expect(screen.queryByText("No active OAuth connections.")).not.toBeInTheDocument();
});
