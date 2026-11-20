import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { inTauri } from "./api";
import "./capture.css";

export interface CaptureExclusionStatus {
  window: string;
  requested: boolean;
  effective: boolean | null;
  windows_build: number;
  error: string | null;
  reason: "off" | "stealth" | "screenshot";
}

/** The saved preference is distinct from Windows actually applying capture exclusion. */
export function CaptureProtectionStatus({ active, enabled }: { active: boolean; enabled: boolean | null }) {
  const [status, setStatus] = useState<Record<string, CaptureExclusionStatus>>({});
  useEffect(() => {
    if (!active || !inTauri) return;
    let disposed = false;
    let off: (() => void) | undefined;
    const received = new Set<string>();
    setStatus({});
    void listen<CaptureExclusionStatus>("capture-exclusion-status", event => {
      if (disposed || !event.payload?.window) return;
      received.add(event.payload.window);
      setStatus(current => ({ ...current, [event.payload.window]: event.payload }));
    }).then(async unsubscribe => {
      if (disposed) { unsubscribe(); return; }
      off = unsubscribe;
      const snapshot = await invoke<CaptureExclusionStatus[]>("capture_exclusion_status");
      if (!disposed && Array.isArray(snapshot)) setStatus(current => ({ ...Object.fromEntries(snapshot.filter(s => s?.window && !received.has(s.window)).map(s => [s.window, s])), ...current }));
    }).catch(() => { /* Older desktop runtimes do not expose verified status. */ });
    return () => { disposed = true; off?.(); };
  }, [active, enabled]);
  const statuses = Object.values(status);
  const failed = statuses.find(s => s.error || (s.effective !== null && s.requested !== s.effective));
  if (failed) return <p className="capture-status-note is-error" role="alert">
    {failed.requested ? "Windows could not hide this app from capture." : "Windows could not restore capture visibility."} {failed.error || "Try the setting again."}
  </p>;
  return null;
}