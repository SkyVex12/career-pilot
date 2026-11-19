import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type Settings } from "./api";
import { HostVoice, VoiceMute } from "./seats";
import type { SessionView } from "./session";
import { useOverlayPopover } from "./overlayPopover";
import { Button, IconButton } from "./ui";
import { Icon } from "./ui/Icon";

/** Capture devices are selected at call start. This panel reports their real activity,
 * without presenting saved settings as a live device switch. */
export function OverlayAudio({ s }: { s: SessionView }) {
  const [open, setOpen] = useState(false), [settings, setSettings] = useState<Settings | null>(null);
  const [devices, setDevices] = useState<Awaited<ReturnType<typeof api.listDevices>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const button = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const position = useOverlayPopover(open, close, button, panel);
  useEffect(() => {
    let active = true;
    void Promise.all([api.getSettings(), api.listDevices()]).then(([v, d]) => { if (active) { setSettings(v?.settings ?? null); setDevices(d); } }, e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [s.sessionId]);
  const name = (kind: "inputs" | "outputs", id: string | null | undefined) => devices?.[kind]?.find(d => d.id === id)?.name || (id ? "Selected device" : "System default");
  return <>
    <IconButton ref={button} label="Live audio" icon={<Icon name="mic" size={15} />} size="sm" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(v => !v)} />
    {open && createPortal(<div ref={panel} className="ov-menu ov-audio-menu" role="dialog" aria-label="Live audio" style={position}>
      <div className="ov-popover-heading"><strong>Live audio</strong><IconButton label="Close live audio" icon={<Icon name="close" size={14} />} size="sm" onClick={() => { close(); button.current?.focus(); }} /></div>
      <div className="ov-audio-source"><Icon name="mic" size={16} /><div><strong>Microphone</strong><span>{name("inputs", settings?.mic_device)}</span></div><span className={s.speaking.me ? "is-speaking" : ""}>{s.speaking.me ? "Speaking" : "Quiet"}</span></div>
      <div className="ov-audio-source"><Icon name="headphones" size={16} /><div><strong>Call audio</strong><span>{name("outputs", settings?.output_device)}</span></div><span className={s.speaking.them ? "is-speaking" : ""}>{s.speaking.them ? "Speaking" : "Quiet"}</span></div>
      {Object.entries(s.status).filter(([, value]) => value.state !== "ok").map(([key, value]) => <p className="ov-menu-hint" role="status" key={key}>{value.message || `${key}: ${value.state}`}</p>)}
      {settings && <span className="ov-menu-hint">Echo cancellation {settings.echo_cancellation ? "on" : "off"}</span>}
      <HostVoice s={s} /><VoiceMute s={s} />
      {error && <p className="ov-ask-error" role="alert">{error}</p>}
      <div className="ov-menu-sep" />
      <Button size="sm" variant="ghost" className="ov-menu-item" onClick={() => { void api.openMainSettings().then(close, e => setError(String(e))); }}>Audio settings…</Button>
      <span className="ov-menu-hint">Device changes apply to the next call.</span>
    </div>, document.body)}
  </>;
}
