import { CaptureProtectionStatus } from "./captureProtection";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, onSettingsChanged, type OverlayPrefs } from "./api";
import { prettyKey } from "./hotkeys";
import type { ViewPrefs } from "./Overlay";
import { useOverlayPopover } from "./overlayPopover";
import { Banner, Button, ConfirmDialog, IconButton, Switch } from "./ui";
import { Icon } from "./ui/Icon";

import { ReadingSettings, TransparencyControl } from "./WindowSettingControls";
import { useHoverMenu } from "./ui/useHoverMenu";

/** Shared preferences, edited from the live overlay. */
export function OverlayQuickSettings({ opacity, onOpacity, onTop, onToggleOnTop, clickThrough, hotkeys, running, guest }: {
  opacity: number; onOpacity: (value: number) => void | Promise<void>;
  onTop: boolean; onToggleOnTop: (on: boolean) => void | Promise<void>;
  clickThrough: boolean; hotkeys: OverlayPrefs["hotkeys"]; running: boolean; guest: boolean; sessionId: string | null;
  view: ViewPrefs; compact: boolean; onCompact: (on: boolean) => void;
}) {
  const [open, setOpen] = useState(false), [quitting, setQuitting] = useState(false);
  const [stealth, setStealth] = useState<boolean | null>(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stealthKey, setStealthKey] = useState("");
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
  const quit = () => api.quitApp();
  return <>
    <IconButton onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} ref={gear} label="Quick settings" icon={<Icon name="settings" size={16} />} size="sm" aria-expanded={open} aria-haspopup="dialog" onClick={() => { hover.cancel(); hoverOpened.current = false; setOpen(v => !v); }} />
    {error && <Banner tone="danger">{error}</Banner>}
    {open && createPortal(<div onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} ref={panel} className="ov-menu ov-quick-settings" role="dialog" aria-label="Quick settings" style={position}>
      <div className="ov-popover-heading"><strong>Quick settings</strong><IconButton label="Close quick settings" icon={<Icon name="close" size={14} />} size="sm" onClick={() => { close(); gear.current?.focus(); }} /></div>
      <TransparencyControl opacity={opacity} onChange={value => void run(async () => { await onOpacity(value); })} />
      <Switch label="Keep on top" checked={onTop} disabled={busy} onChange={on => void run(async () => { await onToggleOnTop(on); })} />
      <Switch label="Click-through" hint={`${prettyKey(hotkeys.click_through_off || "CommandOrControl+Shift+G")} to restore`} checked={clickThrough} onChange={on => void run(() => api.setClickThrough(on))} />
      <div className="ov-menu-sep" />
      <Switch label="Stealth mode" hint={stealthKey ? prettyKey(stealthKey) : undefined} checked={stealth ?? false} disabled={stealth === null || busy} onChange={on => void run(async () => {
        const current = await api.getSettings();
        await api.saveSettings({ ...current.settings, stealth: on }, null); setStealth(on);
      })} />
      <div className="ov-menu-sep" />
      <CaptureProtectionStatus active={open} enabled={stealth} />
      <ReadingSettings running={running} guest={guest} busy={busy} run={action => void run(action)} />
      <div className="ov-menu-sep" />
      {!guest && <Button variant="ghost" size="sm" className="ov-menu-item" onClick={() => { void api.openMainSettings().then(close, e => setError(String(e))); }}>All settings…</Button>}
      <Button variant="danger-ghost" size="sm" className="ov-menu-item" disabled={busy} onClick={() => { if (running) { close(); setQuitting(true); } else void run(quit); }}>Quit CoRuby</Button>
    </div>, document.body)}
    <ConfirmDialog open={quitting} onOpenChange={setQuitting} title={guest ? "Stop assisting and quit CoRuby?" : "Quit during a call?"} confirmLabel={guest ? "Stop assisting & quit" : "Quit CoRuby"} onConfirm={quit}>
      {guest ? "You’ll stop assisting. The other person’s call will continue." : "Your call will end and CoRuby will close."}
    </ConfirmDialog>
  </>;
}
