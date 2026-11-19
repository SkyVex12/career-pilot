import { useEffect } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { inTauri } from "./api";

/** Native bounds belong to the user and explicit mode transitions, never to page content. */
export function useCardWindow() {
  useEffect(() => {
    let disposed = false;
    const sync = () => {
      if (disposed || document.documentElement.hasAttribute("data-window-transition")) return;
      const compact = document.querySelector('.shell-content:not([hidden]) .session-window:not([data-mini="full"])');
      document.documentElement.toggleAttribute("data-card-window", !compact);
      if (!compact && window.innerHeight >= 240) {
        document.documentElement.style.setProperty("--card-max-height", `${window.innerHeight - 16}px`);
      }
    };
    // Only observe the attributes that actually change the visible mode/page. Streaming
    // text, menus and form contents must never schedule native resize operations.
    const root = document.getElementById("root");
    const observer = new MutationObserver(sync);
    if (root) observer.observe(root, { subtree: true, attributes: true, attributeFilter: ["hidden", "data-mini"] });
    window.addEventListener("resize", sync);
    window.addEventListener("copilot:window-layout", sync);
    let unlisten: (() => void) | undefined;
    if (inTauri) void getCurrentWindow().onResized(sync).then(off => {
      if (disposed) off(); else unlisten = off;
    }).catch(() => {});
    sync();
    return () => {
      disposed = true; observer.disconnect(); unlisten?.();
      window.removeEventListener("resize", sync);
      window.removeEventListener("copilot:window-layout", sync);
      document.documentElement.removeAttribute("data-card-window");
    };
  }, []);
}
