import { invoke } from "@tauri-apps/api/core";
import { currentMonitor, cursorPosition, getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalSize, PhysicalPosition, PhysicalSize } from "@tauri-apps/api/dpi";

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
export async function resizeCompactBounds(width: 64 | 280, height: 44 | 56, renderTarget: () => void) {
  const shaped = await invoke<boolean>("set_compact_window_shape", { shape: width === 64 ? "dot" : "toolbar" });
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
    const visualWidth = Math.round(64 * scale), visualHeight = Math.round(44 * scale);
    if (!Number.isFinite(scale) || scale <= 0 || visualWidth <= 0 || visualHeight <= 0 || visualWidth > size.width || visualHeight > size.height) {
      throw new Error("Window pointer bounds are unavailable.");
    }
    const width = Math.min(Math.round(68 * scale), size.width);
    const height = Math.min(Math.round(48 * scale), size.height);
    const left = position.x + Math.round((size.width - width) / 2);
    const top = position.y + Math.round((size.height - height) / 2);
    const x = pointer.x - left, y = pointer.y - top;
    if (x < 0 || y < 0 || x >= width || y >= height) return false;
    const radius = Math.min(10 * scale, width / 2, height / 2);
    const dx = Math.max(radius - x, x - (width - radius), 0);
    const dy = Math.max(radius - y, y - (height - radius), 0);
    return dx * dx + dy * dy < radius * radius;
  }
  // All three APIs use physical pixels, including monitors with negative origins and different DPI.
  return pointer.x >= position.x && pointer.x < right && pointer.y >= position.y && pointer.y < bottom;
}

/** Extend only the medium canvas beneath its toolbar; keep the saved full-mode bounds alone. */
export async function openCompactMenu(): Promise<() => Promise<void>> {
  const win = getCurrentWindow();
  const [position, scale, monitor] = await Promise.all([win.outerPosition(), win.scaleFactor(), currentMonitor()]);
  if (!Number.isFinite(scale) || scale <= 0) throw new Error("Window display scale is unavailable.");
  const work = monitor?.workArea;
  const below = work ? (work.position.y + work.size.height - position.y) / scale - 8 : 480;
  const height = Math.max(56, Math.floor(Math.min(480, below >= 240 ? below : work ? work.size.height / scale - 16 : 480) / 8) * 8);
  const top = work ? Math.max(work.position.y, Math.min(position.y, work.position.y + work.size.height - Math.round((height + 8) * scale))) : position.y;
  const target = new PhysicalPosition(position.x, top);
  const moved = target.y !== position.y;
  const restore = async () => {
    await win.setSize(new LogicalSize(280, 56));
    if (moved) {
      const current = await win.outerPosition();
      // Preserve a deliberate drag while the menu was open.
      if (current.x === target.x && current.y === target.y) await win.setPosition(position);
    }
  };
  try {
    if (moved) await win.setPosition(target);
    await win.setSize(new LogicalSize(280, height));
  } catch (error) {
    await restore();
    throw error;
  }
  return restore;
}
