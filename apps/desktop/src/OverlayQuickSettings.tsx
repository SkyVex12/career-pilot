import { CaptureProtectionStatus } from "./captureProtection";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { availableMonitors, currentMonitor, getCurrentWindow, type Monitor } from "@tauri-apps/api/window";
import { PhysicalPosition } from "@tauri-apps/api/dpi";
import type { AnswerStyle, Mode } from "@copilot/protocol";
import { api, inTauri, onSettingsChanged, rest, type OverlayPrefs } from "./api";
import { prettyKey } from "./hotkeys";
import type { ViewPrefs } from "./Overlay";
import { useOverlayPopover } from "./overlayPopover";
import { Button, ConfirmDialog, IconButton, SegmentedControl, Select, Switch } from "./ui";
import { Icon } from "./ui/Icon";

const monitorId = (m: Monitor) => `${m.name ?? "Display"}:${m.position.x}:${m.position.y}`;

/** Live controls use the overlay's native APIs; Stealth remains an app preference. */
export function OverlayQuickSettings({ opacity, onOpacity, onTop, onToggleOnTop, clickThrough, hotkeys, running, guest, sessionId, view, compact, onCompact }: {
  opacity: number; onOpacity: (value: number) => void;
  onTop: boolean; onToggleOnTop: (on: boolean) => void;
  clickThrough: boolean; hotkeys: OverlayPrefs["hotkeys"]; running: boolean; guest: boolean; sessionId: string | null;
  view: ViewPrefs; compact: boolean; onCompact: (on: boolean) => void;
}) {
  const [open, setOpen] = useState(false), [quitting, setQuitting] = useState(false);
  const [stealth, setStealth] = useState<boolean | null>(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stealthKey, setStealthKey] = useState("");
  const [monitors, setMonitors] = useState<Monitor[]>([]), [display, setDisplay] = useState("");
  const [mode, setMode] = useState<Mode | null>(null);
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>(() => { try { return localStorage.getItem("copilot.answerStyle") === "paragraph" ? "paragraph" : "bullets"; } catch { return "bullets"; } });
  const gear = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const position = useOverlayPopover(open, close, gear, panel);
  useEffect(() => {
    setMode(null);
    if (!sessionId || guest) return;
    let active = true;
    void rest.session(sessionId).then(detail => { if (active) setMode(detail.mode ?? null); }).catch(() => undefined);
    return () => { active = false; };
  }, [sessionId, guest]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    const load = () => api.getSettings().then(v => { if (active) { setStealth(v?.settings?.stealth ?? false); setStealthKey(v?.settings?.toggle_stealth_hotkey ?? ""); } }, e => { if (active) setError(String(e)); });
    void load();
    const off = onSettingsChanged(() => void load());
    if (inTauri) void Promise.all([availableMonitors(), currentMonitor()]).then(([all, current]) => {
      if (!active) return;
      setMonitors(all ?? []); setDisplay(current ? monitorId(current) : "");
    }).catch(() => undefined);
    panel.current?.querySelector<HTMLInputElement>('input[type="range"]')?.focus();
    return () => { active = false; void off.then(f => f()).catch(() => undefined); };
  }, [open]);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await action(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const move = async (id: string) => {
    const monitor = monitors.find(m => monitorId(m) === id);
    if (!monitor || !inTauri) return;
    await run(async () => {
      const window = getCurrentWindow(), size = await window.outerSize();
      await window.setPosition(new PhysicalPosition(monitor.position.x + Math.max(0, Math.round((monitor.size.width - size.width) / 2)), monitor.position.y + Math.max(0, Math.round((monitor.size.height - size.height) / 2))));
      setDisplay(id);
    });
  };
  const quit = async () => { if (guest && running) await api.seatLeave(); await api.quitApp(); };
  return <>
    <IconButton ref={gear} label="Quick settings" icon={<Icon name="settings" size={16} />} size="sm" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(v => !v)} />
    {open && createPortal(<div ref={panel} className="ov-menu ov-quick-settings" role="dialog" aria-label="Quick settings" style={position}>
      <div className="ov-popover-heading"><strong>Quick settings</strong><IconButton label="Close quick settings" icon={<Icon name="close" size={14} />} size="sm" onClick={() => { close(); gear.current?.focus(); }} /></div>
      <label className="ov-menu-row ov-range"><span className="ov-menu-label">Transparency <output>{Math.round((1 - opacity) * 100)}%</output></span>
        <input type="range" aria-label="Transparency" min={0} max={70} step={5} value={Math.round((1 - opacity) * 100)} aria-valuetext={`${Math.round((1 - opacity) * 100)}% transparent`} onChange={e => onOpacity(1 - Number(e.target.value) / 100)} />
      </label>
      <Switch label="Keep on top" checked={onTop} onChange={onToggleOnTop} />
      <Switch label="Click-through" hint={`${prettyKey(hotkeys.click_through_off || "CommandOrControl+Shift+G")} to restore`} checked={clickThrough} onChange={on => void run(() => api.setClickThrough(on))} />
      <div className="ov-menu-sep" />
      <Switch label="Stealth mode" hint={stealthKey ? prettyKey(stealthKey) : undefined} checked={stealth ?? false} disabled={stealth === null || busy} onChange={on => void run(async () => {
        const current = await api.getSettings();
        await api.saveSettings({ ...current.settings, stealth: on }, null); setStealth(on);
      })} />
      <div className="ov-menu-sep" />
      <CaptureProtectionStatus active={open} enabled={stealth} />
      <div className="ov-menu-row"><span className="ov-menu-label">Layout</span><Select aria-label="Layout" value={compact ? "compact" : view.layout} onChange={e => {
        onCompact(e.target.value === "compact"); if (e.target.value !== "compact") view.setLayout(e.target.value as "split" | "stacked");
      }}><option value="split">Split</option><option value="stacked">Stacked</option><option value="compact">Answers only</option></Select></div>
      <div className="ov-menu-row ov-menu-text"><span>Text size <output>{Math.round(view.textScale * 100)}%</output></span><div className="ov-view">
        <IconButton label="Smaller text (Ctrl+minus)" icon="A−" size="sm" onClick={() => view.stepText(-1)} />
        <Button size="sm" variant="ghost" onClick={() => view.stepText(0)}>Reset</Button>
        <IconButton label="Larger text (Ctrl+plus)" icon="A+" size="sm" onClick={() => view.stepText(1)} />
      </div></div>
      {monitors.length > 1 && <div className="ov-menu-row"><span className="ov-menu-label">Display</span><Select aria-label="Display" value={display} disabled={busy} onChange={e => void move(e.target.value)}>{monitors.map((m, i) => <option key={monitorId(m)} value={monitorId(m)}>{m.name || `Display ${i + 1}`}</option>)}</Select></div>}
      {!guest && running && <details className="ov-call-options"><summary>Call options</summary>
        {mode && <div className="ov-menu-row"><span className="ov-menu-label">Call mode</span><Select aria-label="Call mode" value={mode} disabled={busy} onChange={e => void run(async () => { const next = e.target.value as Mode; await api.setMode(next); setMode(next); })}><option value="interview">Interview</option><option value="standup">Standup</option><option value="meeting">Meeting</option></Select></div>}
        <div className="ov-menu-row"><span className="ov-menu-label">Answer format</span><SegmentedControl label="Answer format" value={answerStyle} options={[["bullets", "Bullet points"], ["paragraph", "Paragraphs"]] as const} size="sm" disabled={busy} onChange={value => void run(async () => { await api.setAnswerStyle(value); setAnswerStyle(value); try { localStorage.setItem("copilot.answerStyle", value); } catch { /* Convenience preference. */ } })} /></div>
      </details>}
      {error && <p className="ov-ask-error" role="alert">{error}</p>}
      <div className="ov-menu-sep" />
      {!guest && <Button variant="ghost" size="sm" className="ov-menu-item" onClick={() => { void api.openMainSettings().then(close, e => setError(String(e))); }}>All settings…</Button>}
      <Button variant="danger-ghost" size="sm" className="ov-menu-item" disabled={busy} onClick={() => { if (running) { close(); setQuitting(true); } else void run(quit); }}>Quit Ruby</Button>
    </div>, document.body)}
    <ConfirmDialog open={quitting} onOpenChange={setQuitting} title={guest ? "Stop assisting and quit Ruby?" : "Quit during a call?"} confirmLabel={guest ? "Stop assisting & quit" : "Quit Ruby"} onConfirm={quit}>
      {guest ? "You’ll stop assisting. The other person’s call will continue." : "The call stops on this computer."}
    </ConfirmDialog>
  </>;
}
