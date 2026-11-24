import { CaptureProtectionStatus } from "./captureProtection";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AnswerStyle } from "@copilot/protocol";
import { api, onSettingsChanged, type OverlayPrefs } from "./api";
import { prettyKey } from "./hotkeys";
import type { ViewPrefs } from "./Overlay";
import { useOverlayPopover } from "./overlayPopover";
import { Button, ConfirmDialog, IconButton, SegmentedControl, Switch } from "./ui";
import { Icon } from "./ui/Icon";

import { useHoverMenu } from "./ui/useHoverMenu";

/** Live controls use the overlay's native APIs; Stealth remains an app preference. */
export function OverlayQuickSettings({ opacity, onOpacity, onTop, onToggleOnTop, clickThrough, hotkeys, running, guest, view }: {
  opacity: number; onOpacity: (value: number) => void;
  onTop: boolean; onToggleOnTop: (on: boolean) => void;
  clickThrough: boolean; hotkeys: OverlayPrefs["hotkeys"]; running: boolean; guest: boolean; sessionId: string | null;
  view: ViewPrefs; compact: boolean; onCompact: (on: boolean) => void;
}) {
  const [open, setOpen] = useState(false), [quitting, setQuitting] = useState(false);
  const [stealth, setStealth] = useState<boolean | null>(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stealthKey, setStealthKey] = useState("");
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>(() => { try { return localStorage.getItem("copilot.answerStyle") === "paragraph" ? "paragraph" : "bullets"; } catch { return "bullets"; } });
  const gear = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const cancelHover = useRef(() => {});
  const hoverOpened = useRef(false);
  const close = useCallback(() => { cancelHover.current(); hoverOpened.current = false; setOpen(false); }, []);
  const hover = useHoverMenu({ onOpen: () => { if (!open) { hoverOpened.current = true; setOpen(true); } }, onClose: () => { if (hoverOpened.current) close(); }, keepOpen: () => Boolean(panel.current?.contains(document.activeElement)) });
  cancelHover.current = hover.cancel;
  const position = useOverlayPopover(open, close, gear, panel);
  useEffect(() => {
    if (!open) return;
    if (!hoverOpened.current) panel.current?.querySelector<HTMLInputElement>('input[type="range"]')?.focus();
    let active = true;
    const load = () => api.getSettings().then(v => { if (active) { setStealth(v?.settings?.stealth ?? false); setStealthKey(v?.settings?.toggle_stealth_hotkey ?? ""); } }, e => { if (active) setError(String(e)); });
    void load();
    const off = onSettingsChanged(() => void load());
    return () => { active = false; void off.then(f => f()).catch(() => undefined); };
  }, [open]);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await action(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const quit = async () => { if (guest && running) await api.seatLeave(); await api.quitApp(); };
  return <>
    <IconButton onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} ref={gear} label="Quick settings" icon={<Icon name="settings" size={16} />} size="sm" aria-expanded={open} aria-haspopup="dialog" onClick={() => { hover.cancel(); hoverOpened.current = false; setOpen(v => !v); }} />
    {open && createPortal(<div onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} ref={panel} className="ov-menu ov-quick-settings" role="dialog" aria-label="Quick settings" style={position}>
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
      <div className="ov-menu-row ov-menu-text"><span>Text size <output>{Math.round(view.textScale * 100)}%</output></span><div className="ov-view">
        <IconButton label="Smaller text (Ctrl+minus)" icon="A−" size="sm" onClick={() => view.stepText(-1)} />
        <Button size="sm" variant="ghost" onClick={() => view.stepText(0)}>Reset</Button>
        <IconButton label="Larger text (Ctrl+plus)" icon="A+" size="sm" onClick={() => view.stepText(1)} />
      </div></div>
      {!guest && <>
        <div className="ov-menu-row"><span className="ov-menu-label">Answer format</span><SegmentedControl label="Answer format" value={answerStyle} options={[["bullets", "Bullet points"], ["paragraph", "Paragraphs"]] as const} size="sm" disabled={busy} onChange={value => void run(async () => { if (running) await api.setAnswerStyle(value); setAnswerStyle(value); try { localStorage.setItem("copilot.answerStyle", value); } catch { /* Convenience preference. */ } })} /></div>
      </>}
      {error && <p className="ov-ask-error" role="alert">{error}</p>}
      <div className="ov-menu-sep" />
      {!guest && <Button variant="ghost" size="sm" className="ov-menu-item" onClick={() => { void api.openMainSettings().then(close, e => setError(String(e))); }}>All settings…</Button>}
      <Button variant="danger-ghost" size="sm" className="ov-menu-item" disabled={busy} onClick={() => { if (running) { close(); setQuitting(true); } else void run(quit); }}>Quit CoRuby</Button>
    </div>, document.body)}
    <ConfirmDialog open={quitting} onOpenChange={setQuitting} title={guest ? "Stop assisting and quit CoRuby?" : "Quit during a call?"} confirmLabel={guest ? "Stop assisting & quit" : "Quit CoRuby"} onConfirm={quit}>
      {guest ? "You’ll stop assisting. The other person’s call will continue." : "The call stops on this computer."}
    </ConfirmDialog>
  </>;
}
