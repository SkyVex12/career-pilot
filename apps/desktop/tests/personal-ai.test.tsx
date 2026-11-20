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
