import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Overlay from "./Overlay";
import { api, inTauri } from "./api";
import { throttled } from "./updates";
import "./ui/tokens.css";
import "./styles.css";
import "./ui/ui.css";
import "./screens.css";
import { startTheme } from "./ui/theme";

// One bundle, two windows: the overlay loads index.html#overlay.
const isOverlay = window.location.hash === "#overlay";
document.documentElement.dataset.window = isOverlay ? "overlay" : "main";
// The main window follows the theme (the OS, or the override in Settings); the overlay is
// always dark, since it floats over whatever is on screen.
if (isOverlay) document.documentElement.dataset.theme = "dark";
else startTheme();

// Clicks, keys and scrolls in either window tell the updater the user is here, so an
// automatic update waits for a quiet moment (src-tauri/src/release.rs, schedule).
if (inTauri) {
  const report = throttled(() => void api.noteActivity().catch(() => {}), 20_000);
  for (const type of ["pointerdown", "keydown", "wheel"]) {
    window.addEventListener(type, report, { passive: true, capture: true });
  }
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>{isOverlay ? <Overlay /> : <App />}</React.StrictMode>,
);
