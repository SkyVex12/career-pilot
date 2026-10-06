import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Overlay from "./Overlay";
import "./styles.css";

// One bundle, two windows: the overlay loads index.html#overlay.
const isOverlay = window.location.hash === "#overlay";
document.documentElement.dataset.window = isOverlay ? "overlay" : "main";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>{isOverlay ? <Overlay /> : <App />}</React.StrictMode>,
);
