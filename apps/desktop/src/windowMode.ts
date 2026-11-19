import { animateCompactMorph } from "./compactMotion";
import { startWindowDrag } from "./windowDrag";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalSize, PhysicalSize } from "@tauri-apps/api/dpi";
import { flushSync } from "react-dom";
import { clearCompactShape, isPointerInsideWindow, prepareCompactShape, resizeCompactBounds, resizeWindowBounds, withWindowResize } from "./windowMotion";
import { inTauri } from "./api";

export type WindowMode = "full" | "toolbar" | "dot";
/** Keep the full card open; only compact modes respond to pointer hover. */
export function useWindowMode(overlay = false, autoHover = false) {
  const [mode, setMode] = useState<WindowMode>("full");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saved = useRef<{ size: LogicalSize | PhysicalSize; decorated: boolean; maximized: boolean; resizable: boolean } | null>(null);
  const locked = useRef(false);
  const compactRef = useRef<HTMLElement>(null);
  const motion = useRef<ReturnType<typeof animateCompactMorph> | null>(null);
  const disposed = useRef(false);
  useEffect(() => { disposed.current = false; return () => { disposed.current = true; motion.current?.cancel(); }; }, []);
  const change = async (next: WindowMode) => {
    if (locked.current) return false;
    if (next === mode) return true;
    cancelHover();
    locked.current = true; setBusy(true); setError(null);
    let completed = false;
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
          try {
            const win = getCurrentWindow();
            if (await win.isMinimized()) throw new Error("Restore the window before changing its size.");
            if (!saved.current && next !== "full") {
              const size = await win.innerSize();
              const scale = await win.scaleFactor();
              saved.current = { size: scale > 0 ? size.toLogical(scale) : size, decorated: await win.isDecorated(), maximized: await win.isMaximized(), resizable: await win.isResizable() };
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
              saved.current = null;
            } else {
              if (mode === "full") {
                await prepareCompactShape();
                compactPrepared = true;
                if (await win.isMaximized()) { await win.unmaximize(); unmaximized = true; }
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
              if (cleanupErrors.length) throw new Error(`${String(error)}; could not restore the window: ${cleanupErrors.map(String).join("; ")}`);
              // A later attempt must read the user's current full-window bounds.
              saved.current = null;
            }
            throw error;
          }
        });
      }
      if (disposed.current) return false;
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
      if (!disposed.current) { setBusy(false); if (completed) settleHover(); }
      document.documentElement.removeAttribute("data-window-transition");
      window.dispatchEvent(new Event("copilot:window-layout"));
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
        || generation !== hoverGeneration.current || latest.current.mode !== current;
      if (stale()) return;
      let inside = pointerInside.current;
      if (inTauri) {
        try { inside = await isPointerInsideWindow(current); }
        catch { return; } // Keep the current shape if native pointer state is unavailable.
      }
      if (stale()) return;
      pointerInside.current = inside;
      if (inside === (next === "toolbar")) void latest.current.change(next);
    }, next === "toolbar" ? 160 : 260);
  };
  const settleHover = () => {
    cancelHover();
    if (!autoHover || pointerInside.current === null) return;
    const current = latest.current.mode;
    const next = current === "dot" && pointerInside.current ? "toolbar" : current === "toolbar" && !pointerInside.current ? "dot" : null;
    if (next) scheduleHover(next);
  };
  const onPointerEnter = (event: PointerEvent) => {
    pointerInside.current = true;
    cancelHover();
    if (locked.current || !autoHover || event.pointerType === "touch" || event.buttons || latest.current.mode !== "dot") return;
    scheduleHover("toolbar");
  };
  const onPointerLeave = (event: PointerEvent) => {
    pointerInside.current = false;
    cancelHover();
    if (locked.current || !autoHover || event.pointerType === "touch" || event.buttons || latest.current.mode !== "toolbar") return;
    // Brief grace period allows moving between controls without a collapse flicker.
    scheduleHover("dot");
  };
  const drag = () => { if (inTauri) void startWindowDrag().catch(e => setError(`Could not move the window: ${String(e)}`)); };
  return { mode, busy, error, compactRef, change, drag, onPointerEnter, onPointerLeave, cancelHover };
}
