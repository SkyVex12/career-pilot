import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Icon } from "./ui/Icon";
import "./trayMenu.css";

export function TrayMenu() {
  const [paused, setPaused] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const menu = useRef<HTMLDivElement>(null);
  const focusFirst = () => menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
  useEffect(() => {
    let active = true; let off: (() => void) | undefined;
    void listen<boolean>("copilot://tray-menu", e => { if (active) { setPaused(e.payload); setError(null); focusFirst(); } }).then(async stop => {
      if (!active) { stop(); return; } off = stop;
      try { const value = await invoke<boolean>("tray_menu_ready"); if (active) { setPaused(value); focusFirst(); } }
      catch (e) { if (active) setError(String(e)); }
    });
    window.addEventListener("focus", focusFirst);
    return () => { active = false; off?.(); window.removeEventListener("focus", focusFirst); };
  }, []);
  const act = async (action: string) => {
    if (busy) return; setBusy(true); setError(null);
    try { await invoke("tray_menu_action", { action }); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const key = (e: KeyboardEvent) => {
    if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); void act("dismiss"); return; }
    const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : e.key === "ArrowDown" ? (current + 1) % items.length : e.key === "ArrowUp" ? (current + items.length - 1) % items.length : -1;
    if (next >= 0) { e.preventDefault(); items[next]?.focus(); }
  };
  return <div ref={menu} className="tray-menu" role="menu" aria-label="CoRuby" onKeyDown={key}>
    <button type="button" role="menuitem" disabled={busy} onClick={() => void act("pause")}><span aria-hidden="true">{paused ? "▶" : "Ⅱ"}</span>{error ? <span className="tray-error" role="alert" title={error}>Try again</span> : paused ? "Resume suggestions" : "Pause suggestions"}</button>
    <button type="button" role="menuitem" className="tray-exit" disabled={busy} onClick={() => void act("exit")}><Icon name="power" size={14} />Exit</button>
  </div>;
}
