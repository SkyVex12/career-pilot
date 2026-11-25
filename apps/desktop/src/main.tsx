import React from "react";
import { installWindowDragging } from "./windowDrag";
import ReactDOM from "react-dom/client";
import { api, inTauri } from "./api";
import { throttled } from "./updates";
import "./ui/tokens.css";
import "./overlay.css";
import "./ui/ui.css";
import "./screens.css";
import "./workspace.css";
import "./desktop.css";
import { startTheme } from "./ui/theme";
import { installScrollAffordances } from "./ui/scrollAffordances";
import "./ui/scrollAffordances.css";

// One bundle, two windows: the overlay loads index.html#overlay.
const isTray = new URLSearchParams(window.location.search).has("tray-menu");
const isSuggestion = new URLSearchParams(window.location.search).has("call-suggestion");
const isOverlay = window.location.hash === "#overlay";
document.documentElement.dataset.window = isTray ? "tray" : isSuggestion ? "suggestion" : isOverlay ? "overlay" : "main";
// Both windows follow the chosen theme and stay in sync when it changes.
startTheme();
const stopScrollAffordances = installScrollAffordances();
if (import.meta.hot) import.meta.hot.dispose(stopScrollAffordances);

// Clicks, keys and scrolls in either window tell the updater the user is here, so an
// automatic update waits for a quiet moment (src-tauri/src/release.rs, schedule).
if (inTauri && !isSuggestion && !isTray) {
  const stopDragging = installWindowDragging();
  if (import.meta.hot) import.meta.hot.dispose(stopDragging);
  const report = throttled(() => void api.noteActivity().catch(() => {}), 20_000);
  for (const type of ["pointerdown", "keydown", "wheel"]) {
    window.addEventListener(type, report, { passive: true, capture: true });
  }
}

// A login-only launch must not mount App hooks that fetch private data or wake the VM.
// Subscribe before reading state so an Open action cannot be lost during startup.
if (inTauri && !isOverlay && !isSuggestion && !isTray) {
  const { invoke } = await import("@tauri-apps/api/core");
  const { listen } = await import("@tauri-apps/api/event");
  await new Promise<void>((resolve) => {
    let done = false;
    let unlisten: (() => void) | undefined;
    const check = async () => {
      try {
        const state = await invoke<{ background_start: boolean }>("background_status");
        if (!state.background_start && !done) { done = true; unlisten?.(); resolve(); }
      } catch { if (!done) { done = true; unlisten?.(); resolve(); } }
    };
    void listen("copilot://background", () => void check()).then(stop => { unlisten = stop; if (done) stop(); else void check(); }).catch(() => void check());
  });
}

// Each window loads only its own screens: the overlay starts without the main window's code.
const Root = isTray ? (await import("./TrayMenu")).TrayMenu : isSuggestion ? (await import("./BackgroundSuggestion")).BackgroundSuggestion : isOverlay ? (await import("./Overlay")).default : (await import("./App")).default;
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
