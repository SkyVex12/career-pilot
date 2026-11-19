import { invoke } from "@tauri-apps/api/core";
import { cursorPosition, getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalSize, PhysicalSize } from "@tauri-apps/api/dpi";

let pending: Promise<unknown> = Promise.resolve();
/** Native window updates must never overlap auto-fit and a user transition. */
export function withWindowResize<T>(operation: () => Promise<T>): Promise<T> {
  const result = pending.then(operation);
  pending = result.catch(() => {});
  return result;
}

/** Apply a mode's bounds once. Animating native bounds or cloned DOM makes WebView2
 * repaint intermediate sizes and can race minimize/restore. Keep the live tree mounted. */
export async function resizeWindowBounds(target: LogicalSize | PhysicalSize, renderTarget: () => void = () => {}) {
  const win = getCurrentWindow();
  await win.setSize(target);
  renderTarget();
}

/** Windows keeps the compact WebView surface fixed and changes only its visible/input region. */
export async function resizeCompactBounds(width: 48 | 280, height: 48 | 56, renderTarget: () => void) {
  const shaped = await invoke<boolean>("set_compact_window_shape", { shape: width === 48 ? "dot" : "toolbar" });
  if (shaped !== true) await invoke("resize_compact_window", { width, height });
  renderTarget();
}

/** Protect the compact frame before the first native resize; leave bounds unchanged. */
export async function prepareCompactShape() {
  await invoke("set_compact_window_shape", { shape: "toolbar" });
}

/** Remove the compact input region before restoring the full workspace. */
export async function clearCompactShape() {
  await invoke("set_compact_window_shape", { shape: "full" });
}

/** Compare native coordinates after a resize; DOM hover events can describe the old bounds. */
export async function isPointerInsideWindow(mode: "toolbar" | "dot" = "toolbar"): Promise<boolean> {
  const win = getCurrentWindow();
  const [pointer, position, size] = await Promise.all([cursorPosition(), win.innerPosition(), win.innerSize()]);
  const right = position.x + size.width;
  const bottom = position.y + size.height;
  if (![pointer.x, pointer.y, position.x, position.y, size.width, size.height, right, bottom].every(Number.isFinite)
    || size.width <= 0 || size.height <= 0) {
    throw new Error("Window pointer bounds are unavailable.");
  }
  if (mode === "dot") {
    const scale = await win.scaleFactor();
    const visualDiameter = Math.round(48 * scale);
    if (!Number.isFinite(scale) || scale <= 0 || visualDiameter <= 0 || visualDiameter > Math.min(size.width, size.height)) {
      throw new Error("Window pointer bounds are unavailable.");
    }
    // Windows leaves 2px around the CSS circle so its antialiased edge is not
    // cut by the native region. Include that small hit margin in hover checks;
    // platforms with a 48px native window still use their actual bounds.
    const diameter = Math.min(Math.round(52 * scale), size.width, size.height);
    const radius = diameter / 2;
    const centerX = position.x + Math.round((size.width - diameter) / 2) + radius;
    const centerY = position.y + Math.round((size.height - diameter) / 2) + radius;
    return Math.hypot(pointer.x - centerX, pointer.y - centerY) < radius;
  }
  // All three APIs use physical pixels, including monitors with negative origins and different DPI.
  return pointer.x >= position.x && pointer.x < right && pointer.y >= position.y && pointer.y < bottom;
}
