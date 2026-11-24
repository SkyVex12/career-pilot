import { act, render, screen } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { afterEach, expect, it, vi } from "vitest";
import { CatalogProvider, useCatalog } from "../src/Applications";
import { DataActivity, usePolling } from "../src/usePolling";
import { ipc } from "./ipc";

afterEach(() => vi.useRealTimers());

function Poll({ read, active = true }: { read: () => unknown; active?: boolean }) {
  usePolling(read, 1000, active, { timeoutMs: 5000 });
  return null;
}

it("never overlaps slow reads, including across a hide/show transition", async () => {
  vi.useFakeTimers();
  let finish!: () => void;
  const read = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
  const view = render(<Poll read={read} />);
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(read).toHaveBeenCalledTimes(1);
  view.rerender(<Poll read={read} active={false} />);
  view.rerender(<Poll read={read} />);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => finish());
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(read).toHaveBeenCalledTimes(2);
});

it("stops a hidden screen, backs off failures, and honors a deadline despite rerenders", async () => {
  vi.useFakeTimers();
  const read = vi.fn(async () => false);
  const view = render(<DataActivity.Provider value={false}><Poll read={read} /></DataActivity.Provider>);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(read).not.toHaveBeenCalled();
  view.rerender(<DataActivity.Provider value><Poll read={() => read()} /></DataActivity.Provider>);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(read).toHaveBeenCalledTimes(1);
  view.rerender(<DataActivity.Provider value><Poll read={() => read()} /></DataActivity.Provider>);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(read).toHaveBeenCalledTimes(2);
});

function CatalogReader({ label }: { label: string }) {
  const catalog = useCatalog();
  return <output aria-label={label}>{catalog.applications?.length ?? "loading"}</output>;
}

it("shares a single catalog and does not poll missing, expired, or failed packs", async () => {
  vi.useFakeTimers();
  ipc.api("GET", /^\/applications$/, () => ({ applications: [
    { id: "missing", pack: null },
    { id: "expired", pack: { available: false, availability: "expired", operation_state: "idle" } },
    { id: "failed", pack: { available: false, operation_state: "failed" } },
  ] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  await act(async () => render(<CatalogProvider enabled><CatalogReader label="setup" /><CatalogReader label="prep" /></CatalogProvider>));
  expect(screen.getByLabelText("setup")).toHaveTextContent("3");
  expect(screen.getByLabelText("prep")).toHaveTextContent("3");
  await act(async () => vi.advanceTimersByTimeAsync(60_000));
  expect(ipc.apiCalls("GET")).toHaveLength(2);
});

it("polls known catalog operations without waking the server and drops an old profile read", async () => {
  vi.useFakeTimers();
  let finish!: (value: unknown) => void, reads = 0;
  ipc.api("GET", /^\/applications$/, () => ++reads === 2 ? new Promise(resolve => { finish = resolve; }) : ({ applications: reads === 1 ? [{ id: "pending", pack: { available: false, operation_state: "running" } }] : [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/applications\/status$/, () => ({ applications: [{ id: "pending", resume_status: null, pack: { available: true, operation_state: "succeeded" } }], interviews: [] }));
  const view = render(<CatalogProvider key="first" enabled><CatalogReader label="catalog" /></CatalogProvider>);
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(4000));
  expect(ipc.calls.filter(call => call.args.path === "/applications").slice(-1)[0]?.args).toMatchObject({ refresh: true, wake: false });
  await act(async () => view.rerender(<CatalogProvider key="second" enabled><CatalogReader label="catalog" /></CatalogProvider>));
  expect(screen.getByLabelText("catalog")).toHaveTextContent("0");
  await act(async () => finish({ applications: [{ id: "private-old" }] }));
  expect(screen.getByLabelText("catalog")).toHaveTextContent("0");
});

it("checks catalog metadata without retransmitting unchanged application lists", async () => {
  vi.useFakeTimers();
  const pack = { available: false, availability: "missing", operation_state: "running" };
  ipc.api("GET", /^\/applications$/, () => ({ applications: [{ id: "pending", pack }] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/applications\/status$/, () => ({ applications: [{ id: "pending", resume_status: null, pack }], interviews: [] }));
  await act(async () => render(<CatalogProvider enabled><CatalogReader label="catalog" /></CatalogProvider>));
  await act(async () => vi.advanceTimersByTimeAsync(12_000));
  expect(ipc.apiCalls("GET").filter(call => call.path === "/applications")).toHaveLength(1);
  expect(ipc.apiCalls("GET").filter(call => call.path === "/interviews")).toHaveLength(1);
  expect(ipc.apiCalls("GET").filter(call => call.path === "/applications/status")).toHaveLength(3);
});

it("debounces catalog writes from other windows and defers hidden refreshes", async () => {
  vi.useFakeTimers();
  ipc.api("GET", /^\/applications$/, () => ({ applications: [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  const view = render(<CatalogProvider enabled><CatalogReader label="catalog" /></CatalogProvider>);
  await act(async () => {});
  await act(async () => { await emit("copilot://data-changed", "/applications/a"); await emit("copilot://data-changed", "/resumes/b"); });
  await act(async () => vi.advanceTimersByTimeAsync(150));
  expect(ipc.apiCalls("GET").filter(call => call.path === "/applications")).toHaveLength(2);
  expect(ipc.calls.filter(call => call.args.path === "/applications").slice(-1)[0].args.wake).toBe(false);
  view.rerender(<CatalogProvider enabled={false}><CatalogReader label="catalog" /></CatalogProvider>);
  await act(async () => { await emit("copilot://data-changed", "/applications/a"); await vi.advanceTimersByTimeAsync(500); });
  expect(ipc.apiCalls("GET").filter(call => call.path === "/applications")).toHaveLength(2);
  await act(async () => view.rerender(<CatalogProvider enabled><CatalogReader label="catalog" /></CatalogProvider>));
  expect(ipc.apiCalls("GET").filter(call => call.path === "/applications")).toHaveLength(3);
});

it("revalidates a settled shared catalog once on foreground return", async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  ipc.api("GET", /^\/applications$/, () => ({ applications: [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  try {
    await act(async () => render(<CatalogProvider enabled><CatalogReader label="setup" /><CatalogReader label="prep" /></CatalogProvider>));
    await act(async () => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")); });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(ipc.apiCalls("GET")).toHaveLength(2);
    await act(async () => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
    expect(ipc.apiCalls("GET")).toHaveLength(4);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(ipc.apiCalls("GET")).toHaveLength(4);
  } finally { visibility.mockRestore(); }
});
