import { useCallback, useEffect, useRef, useState } from "react";
import { emit, listen } from "@tauri-apps/api/event";
import { inTauri } from "./api";

export type ViewerWindow = "main" | "overlay";
type Claim = { seatId: string; id: string; window: ViewerWindow; stamp: number };
type ViewerEvent = Claim & { type: "claim" | "release" };
const EVENT = "screen:viewer";

/** A seat has one LiveKit identity. Main and overlay must hand the receiver over rather
 * than connect twice and repeatedly evict one another from the media room. */
export function useScreenViewer(seatId: string | null, active: boolean, window: ViewerWindow) {
  const [ownership, setOwnership] = useState<{ seatId: string; ours: boolean; window: ViewerWindow } | null>(null);
  const take = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (!seatId || !active) { setOwnership(null); return; }
    if (!inTauri) { setOwnership({ seatId, ours: true, window }); return; }
    const id = crypto.randomUUID();
    let live = true;
    let latest: Claim | null = null;
    let off: (() => void) | undefined;
    const claim = () => {
      if (!live) return;
      latest = { seatId, id, window, stamp: Math.max(Date.now(), (latest?.stamp ?? 0) + 1) };
      setOwnership({ seatId, ours: true, window });
      void emit(EVENT, { ...latest, type: "claim" } satisfies ViewerEvent);
    };
    take.current = claim;
    void listen<ViewerEvent>(EVENT, ({ payload }) => {
      if (!live || payload.seatId !== seatId) return;
      if (payload.type === "release") {
        if (latest?.id === payload.id && latest.stamp === payload.stamp && payload.id !== id) claim();
        return;
      }
      if (latest && (payload.stamp < latest.stamp || (payload.stamp === latest.stamp && payload.id < latest.id))) return;
      latest = payload;
      setOwnership({ seatId, ours: payload.id === id, window: payload.window });
    }).then(unlisten => {
      if (!live) { unlisten(); return; }
      off = unlisten;
      claim();
    });
    return () => {
      live = false;
      take.current = null;
      off?.();
      if (latest?.id === id) void emit(EVENT, { ...latest, type: "release" } satisfies ViewerEvent);
    };
  }, [seatId, active, window]);
  const takeHere = useCallback(() => take.current?.(), []);
  return {
    owns: active && ownership?.seatId === seatId && ownership.ours,
    elsewhere: active && ownership?.seatId === seatId && !ownership.ours ? ownership.window : null,
    takeHere,
  };
}
