// Test setup. Component tests (*.test.tsx, jsdom) get Testing Library's matchers, cleanup after
// each test, and the Rust core faked through Tauri's mockIPC before src/api.ts is imported, so
// `inTauri` is true and every command goes to the handler a test sets with `ipc.handle`.
import type {} from "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

if (typeof window !== "undefined") {
  await import("@testing-library/jest-dom/vitest");
  const { cleanup } = await import("@testing-library/react");
  const { mockIPC } = await import("@tauri-apps/api/mocks");
  const { ipc } = await import("./ipc");
  mockIPC((cmd, args) => ipc.dispatch(cmd, (args ?? {}) as Record<string, unknown>), { shouldMockEvents: true });
  // jsdom has no matchMedia, scrollIntoView or ResizeObserver; the UI only needs them to exist.
  window.matchMedia ??= ((query: string) =>
    ({ matches: false, media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false }) as unknown as MediaQueryList);
  Element.prototype.scrollIntoView ??= function () {};
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => {};
  Element.prototype.releasePointerCapture ??= () => {};
  window.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  afterEach(() => {
    cleanup();
    ipc.reset();
    localStorage.clear();
  });
}
