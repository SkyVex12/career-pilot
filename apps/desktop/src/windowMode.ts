import { animateCompactMorph } from "./compactMotion";
import { startWindowDrag } from "./windowDrag";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalSize, PhysicalSize } from "@tauri-apps/api/dpi";
import { flushSync } from "react-dom";
import { clearCompactShape, isPointerInsideWindow, openCompactMenu, prepareCompactShape, resizeCompactBounds, resizeWindowBounds, withWindowResize } from "./windowMotion";
import { inTauri } from "./api";

export type WindowMode = "full" | "toolbar" | "dot";
/** Keep full mode open; compact mode stays expanded while focused or hovered. */
export function useWindowMode(overlay = false, autoHover = false) {
  const [mode, setMode] = useState<WindowMode>("full");
  const [busy, setBusy] = useState(false);
  const [menuOpen, renderMenu] = useState(false);
  const menuVisible = useRef(false);
  const menuWanted = useRef(false);
  const restoreMenu = useRef<(() => Promise<void>) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const saved = useRef<{ size: LogicalSize | PhysicalSize; decorated: boolean; maximized: boolean; fullscreen: boolean; resizable: boolean } | null>(null);
  const locked = useRef(false);
  const compactRef = useRef<HTMLElement>(null);
  const motion = useRef<ReturnType<typeof animateCompactMorph> | null>(null);
  const disposed = useRef(false);
  useEffect(() => { disposed.current = false; return () => { disposed.current = true; motion.current?.cancel(); }; }, []);
  const focused = useRef<boolean | null>(null);
  const change = async (next: WindowMode, automatic = false) => {
    if (locked.current) return false;
    if (next === mode) return true;
    if ((menuVisible.current || menuWanted.current) && !await setMenuOpen(false)) return false;
    if (locked.current) return false;
    cancelHover();
    locked.current = true; setBusy(true); setError(null);
    let completed = false;
    let cancelled = false;
    document.documentElement.setAttribute("data-window-transition", "true");
    try {
      if (inTauri) {
        await withWindowResize(async () => {
          let compactPrepared = false;
          let compactRendered = false;
          let resizeLocked = false;
          let constraintsChanged = false;
          let decorationsChanged = false;
          let unmaximized = false;
          let exitedFullscreen = false;
          try {
            const win = getCurrentWindow();
            if (await win.isMinimized()) throw new Error("Restore the window before changing its size.");
            if (!saved.current && next !== "full") {
              const size = await win.innerSize();
              const scale = await win.scaleFactor();
              saved.current = { size: scale > 0 ? size.toLogical(scale) : size, decorated: await win.isDecorated(), maximized: await win.isMaximized(), fullscreen: Boolean(await win.isFullscreen()), resizable: await win.isResizable() };
            }
            if (next === "full") {
              await clearCompactShape();
              const decorated = saved.current?.decorated ?? !overlay;
              if (await win.isDecorated() !== decorated) await win.setDecorations(decorated);
              await win.setMinSize(new LogicalSize(44, 44));
              if (saved.current) await resizeWindowBounds(saved.current.size, () => flushSync(() => setMode(next)));
              await win.setMinSize(new LogicalSize(overlay ? 320 : 400, 240));
              if (saved.current && await win.isResizable() !== saved.current.resizable) await win.setResizable(saved.current.resizable);
              if (saved.current?.maximized) await win.maximize();
              if (saved.current?.fullscreen) await win.setFullscreen(true);
              saved.current = null;
            } else {
              if (mode === "full") {
                await prepareCompactShape();
                compactPrepared = true;
                if (saved.current?.fullscreen) { await win.setFullscreen(false); exitedFullscreen = true; }
                if (await win.isMaximized()) { await win.unmaximize(); unmaximized = true; }
                if (saved.current && (unmaximized || exitedFullscreen)) {
                  // Save restored bounds, not the monitor-sized maximized/fullscreen frame.
                  // Otherwise the next Restore action turns into another fullscreen-sized window.
                  const size = await win.innerSize();
                  const scale = await win.scaleFactor();
                  saved.current.size = scale > 0 ? size.toLogical(scale) : size;
                }
                if (saved.current?.resizable) {
                  await win.setResizable(false);
                  resizeLocked = true;
                }
                await win.setMinSize(new LogicalSize(44, 44));
                constraintsChanged = true;
                if (await win.isDecorated()) { await win.setDecorations(false); decorationsChanged = true; }
              }
              if (mode === "toolbar" && next === "dot") {
                motion.current = animateCompactMorph(compactRef.current, true);
                if (!await motion.current.finished || disposed.current) throw new Error("Window transition interrupted.");
                if (await win.isMinimized()) throw new Error("Restore the window before changing its size.");
                // A click can focus the toolbar while its collapse animation is running.
                if (automatic && focused.current !== false) { cancelled = true; return; }
                await resizeCompactBounds(64, 44, () => {
                  flushSync(() => setMode(next));
                  motion.current?.cancel();
                });
              } else if (mode === "dot" && next === "toolbar") {
                await resizeCompactBounds(280, 56, () => {
                  flushSync(() => setMode(next));
                  motion.current = animateCompactMorph(compactRef.current, false);
                });
                await motion.current?.finished;
              } else {
                await resizeWindowBounds(new LogicalSize(280, 56), () => {
                  flushSync(() => setMode("toolbar"));
                  compactRendered = true;
                });
                if (next === "dot") await resizeCompactBounds(64, 44, () => flushSync(() => setMode(next)));
              }
            }
          } catch (error) {
            if (mode === "full" && !compactRendered) {
              // A failed entry still shows the full card. Restore its native
              // options within this serialized operation before dropping the guard.
              const restore = saved.current;
              const cleanupErrors: unknown[] = [];
              const win = getCurrentWindow();
              if (resizeLocked && restore) {
                try { await win.setResizable(restore.resizable); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (decorationsChanged && restore) {
                try { await win.setDecorations(restore.decorated); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (constraintsChanged) {
                try { await win.setMinSize(new LogicalSize(overlay ? 320 : 400, 240)); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (compactPrepared) {
                try { await clearCompactShape(); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (unmaximized && restore?.maximized) {
                try { await win.maximize(); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (exitedFullscreen && restore?.fullscreen) {
                try { await win.setFullscreen(true); }
                catch (cleanupError) { cleanupErrors.push(cleanupError); }
              }
              if (cleanupErrors.length) throw new Error(`${String(error)}; could not restore the window: ${cleanupErrors.map(String).join("; ")}`);
              // A later attempt must read the user's current full-window bounds.
              saved.current = null;
            }
            throw error;
          }
        });
      }
      if (disposed.current || cancelled) return false;
      flushSync(() => setMode(next));
      completed = true;
      return true;
    } catch (e) {
      if (!disposed.current) setError(`Could not resize the window: ${String(e)}`);
      // A failed native resize must not put compact content into full-size bounds.
      return false;
    } finally {
      motion.current?.cancel(); motion.current = null;
      locked.current = false;
      if (!disposed.current) { setBusy(false); if (completed || cancelled) settleHover(); }
      document.documentElement.removeAttribute("data-window-transition");
      window.dispatchEvent(new Event("copilot:window-layout"));
    }
  };
  const setMenuOpen = async (open: boolean): Promise<boolean> => {
    menuWanted.current = open;
    cancelHover();
    try {
      return await withWindowResize(async () => {
        if (disposed.current || locked.current || open !== menuWanted.current) return false;
        if (menuVisible.current === open) return true;
        if (open && latest.current.mode !== "toolbar") { menuWanted.current = false; return false; }
        if (open) {
          // Pin the toolbar before extending its transparent canvas downward.
          flushSync(() => { menuVisible.current = true; renderMenu(true); });
          try {
            if (inTauri) restoreMenu.current = await openCompactMenu();
          } catch (error) {
            flushSync(() => { menuVisible.current = false; renderMenu(false); });
            throw error;
          }
        } else {
          await restoreMenu.current?.();
          restoreMenu.current = null;
          flushSync(() => { menuVisible.current = false; renderMenu(false); });
          settleHover();
        }
        return true;
      });
    } catch (error) {
      menuWanted.current = menuVisible.current;
      if (!disposed.current) setError("Could not open quick settings: " + String(error));
      return false;
    }
  };
  const latest = useRef({ mode, change, autoHover });
  latest.current = { mode, change, autoHover };
  const pointerInside = useRef<boolean | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const hoverGeneration = useRef(0);
  const cancelHover = () => {
    hoverGeneration.current++;
    clearTimeout(hoverTimer.current);
    hoverTimer.current = undefined;
  };
  useEffect(() => cancelHover, [autoHover]);
  const scheduleHover = (next: "toolbar" | "dot") => {
    cancelHover();
    const generation = hoverGeneration.current;
    hoverTimer.current = setTimeout(async () => {
      hoverTimer.current = undefined;
      const current = next === "toolbar" ? "dot" : "toolbar";
      const stale = () => disposed.current || locked.current || !latest.current.autoHover
        || menuVisible.current || menuWanted.current || (next === "dot" && focused.current !== false)
        || generation !== hoverGeneration.current || latest.current.mode !== current;
      if (stale()) return;
      let inside = pointerInside.current;
      if (inTauri && !(next === "toolbar" && focused.current)) {
        try { inside = await isPointerInsideWindow(current); }
        catch { return; } // Keep the current shape if native pointer state is unavailable.
      }
      if (stale()) return;
      pointerInside.current = inside;
      if (inside === (next === "toolbar") || (next === "toolbar" && focused.current)) void latest.current.change(next, true);
    }, next === "toolbar" ? 160 : 260);
  };
  const settleHover = () => {
    cancelHover();
    if (!latest.current.autoHover || menuVisible.current || menuWanted.current) return;
    const current = latest.current.mode;
    const next = current === "dot" && (focused.current || pointerInside.current) ? "toolbar"
      : current === "toolbar" && focused.current === false && pointerInside.current !== true ? "dot" : null;
    if (next) scheduleHover(next);
  };
  useEffect(() => {
    if (!autoHover) return;
    focused.current = null;
    let active = true;
    let generation = 0;
    let unlisten: (() => void) | undefined;
    const update = (value: boolean) => {
      if (!active) return;
      generation++;
      focused.current = value;
      // A native blur can arrive without a DOM pointer-leave event.
      if (!value) pointerInside.current = null;
      settleHover();
    };
    if (inTauri) {
      // Listen before querying so a late initial response cannot overwrite a click.
      void (async () => {
        const win = getCurrentWindow();
        const stop = await win.onFocusChanged(event => update(event.payload));
        if (!active) { stop(); return; }
        unlisten = stop;
        const requested = generation;
        try {
          const value = await win.isFocused();
          if (active && generation === requested) update(Boolean(value));
        } catch { /* Keep the toolbar open when native focus is unknown. */ }
      })().catch(() => {});
    } else {
      const focus = () => update(true), blur = () => update(false);
      window.addEventListener("focus", focus);
      window.addEventListener("blur", blur);
      update(document.hasFocus());
      unlisten = () => { window.removeEventListener("focus", focus); window.removeEventListener("blur", blur); };
    }
    return () => { active = false; unlisten?.(); cancelHover(); };
    // Event handlers use refs so mode changes do not re-register native listeners.
  }, [autoHover]);
  const onPointerEnter = (event: PointerEvent) => {
    pointerInside.current = true;
    cancelHover();
    if (locked.current || !autoHover || event.pointerType === "touch" || event.buttons || latest.current.mode !== "dot") return;
    scheduleHover("toolbar");
  };
  const onPointerLeave = (event: PointerEvent) => {
    pointerInside.current = false;
    cancelHover();
    if (locked.current || !autoHover || event.pointerType === "touch" || event.buttons || focused.current !== false || latest.current.mode !== "toolbar") return;
    // Brief grace period allows moving between controls without a collapse flicker.
    scheduleHover("dot");
  };
  const drag = () => { if (inTauri) void startWindowDrag().catch(e => setError(`Could not move the window: ${String(e)}`)); };
  return { mode, busy, error, menuOpen, setMenuOpen, compactRef, change, drag, onPointerEnter, onPointerLeave, cancelHover };
}
