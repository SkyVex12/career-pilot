// Pending screenshots float above the composer; capture and solve hotkeys stay unchanged.
import { useEffect, useState } from "react";
import { api, inTauri, onShotError, onShots, type ShotView } from "./api";
import { useToast } from "./ui";

export function shotTitle(s: ShotView): string {
  const kb = Math.max(1, Math.round(s.bytes / 1024));
  const from = s.source === "window" ? s.title || "window" : "screen";
  return `${from} · ${s.width}×${s.height} · ${kb} KB · ${s.capture_ms + s.encode_ms} ms`;
}


export function useScreenshotTray(running: boolean) {
  const [shots, setShots] = useState<ShotView[]>([]);
  const toast = useToast();
  useEffect(() => {
    if (!inTauri) return;
    let active = true; let updated = false;
    void api.codingShots().then(s => { if (active && !updated) setShots(Array.isArray(s) ? s : []); }, () => undefined);
    const offs = [onShots(s => { updated = true; if (active) setShots(s); }), onShotError(error => { if (active) toast({ tone:"error", text:error }); })];
    return () => { active = false; offs.forEach(p => void p.then(off => off())); };
  }, [running, toast]);
  return shots;
}

export function CodingBar({ shots, disabled }: { shots: ShotView[]; disabled?: boolean }) {
  const toast = useToast();
  if (!shots.length) return null;
  return <ol className="ov-screenshot-tray" aria-label="Screenshots ready to answer">
    {shots.map((shot, i) => <li key={shot.id} className="ov-shot" title={shotTitle(shot)}>
      <img src={shot.url} alt={"Screenshot " + (i + 1) + ": " + (shot.title || "screen")} />
      <button type="button" className="ov-shot-x" aria-label={"Remove screenshot " + (i + 1)} disabled={disabled} onClick={() => void api.codingRemoveShot(shot.id).catch(e => toast({ tone:"error", text:String(e) }))}>×</button>
    </li>)}
  </ol>;
}
