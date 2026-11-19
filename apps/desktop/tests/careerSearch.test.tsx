import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PullDialog } from "../src/CareerOs";
import type { CareerOsItem } from "../src/api";
import { ipc } from "./ipc";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
const advance = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const searches = () => ipc.apiCalls("GET").filter(call => call.path.startsWith("/career-os/applications"));
const item = (name: string): CareerOsItem => ({
  id: name, company: name, role: "Engineer", stage: "applied", source_site: null, url: null,
  created_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:00:00Z", user: null,
  profile_id: null, resume: "none", pulled: null,
});
const mount = () => render(<PullDialog open onOpenChange={() => {}} onPulled={() => {}} />);

it("searches after typing pauses and immediately after a stage change, without a Search button", async () => {
  ipc.api("GET", /^\/career-os\/applications$/, () => ({ items: [], next_cursor: null }));
  mount();
  await advance();
  expect(searches()).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Search" })).not.toBeInTheDocument();
  const input = screen.getByRole("searchbox", { name: "Company or role" });
  fireEvent.change(input, { target: { value: "Ced" } });
  await advance(150);
  fireEvent.change(input, { target: { value: "Cedar" } });
  await advance(299);
  expect(searches()).toHaveLength(1);
  await advance(1);
  expect(searches().slice(-1)[0]?.path).toBe("/career-os/applications?q=Cedar");
  fireEvent.change(input, { target: { value: "Cedar team" } });
  fireEvent.click(screen.getByRole("combobox", { name: "Stage" }));
  fireEvent.click(screen.getByRole("option", { name: "Interviewing" }));
  await advance();
  expect(searches().slice(-1)[0]?.path).toBe("/career-os/applications?q=Cedar%20team&stage=interview");
  await advance(350);
  expect(searches()).toHaveLength(3);
});

it("ignores an older search response after a newer query has finished", async () => {
  let resolveOld!: (value: { items: CareerOsItem[] }) => void;
  ipc.api("GET", /^\/career-os\/applications$/, path => {
    if (path.includes("q=old")) return new Promise(resolve => { resolveOld = resolve; });
    return { items: path.includes("q=new") ? [item("New result")] : [] };
  });
  mount();
  await advance();
  const input = screen.getByRole("searchbox", { name: "Company or role" });
  fireEvent.change(input, { target: { value: "old" } });
  await advance(300);
  fireEvent.change(input, { target: { value: "new" } });
  await advance(300);
  expect(screen.getByText("New result · Engineer")).toBeInTheDocument();
  await act(async () => { resolveOld({ items: [item("Old result")] }); });
  expect(screen.queryByText("Old result · Engineer")).not.toBeInTheDocument();
  expect(screen.getByText("New result · Engineer")).toBeInTheDocument();
});

it("retries a failed query immediately while keeping its text", async () => {
  let fail = false;
  ipc.api("GET", /^\/career-os\/applications$/, () => {
    if (fail) throw "Temporarily unavailable";
    return { items: [] };
  });
  mount();
  await advance();
  fail = true;
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Cedar" } });
  await advance(300);
  expect(screen.getByRole("alert")).toHaveTextContent("Temporarily unavailable");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await advance();
  expect(searches()).toHaveLength(3);
  expect(searches().slice(-1)[0]?.path).toBe("/career-os/applications?q=Cedar");
  expect(screen.getByRole("searchbox")).toHaveValue("Cedar");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
