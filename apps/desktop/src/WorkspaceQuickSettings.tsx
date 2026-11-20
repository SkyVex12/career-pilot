import { CaptureProtectionStatus } from "./captureProtection";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { api, onSettingsChanged, onWorkspaceWindowPrefs, type OverlayPrefs } from "./api";
import { prettyKey } from "./hotkeys";
import { isMac } from "./platform";
import { Button, ConfirmDialog, IconButton, Switch } from "./ui";
import { useHoverMenu, WORKSPACE_MENU_OPEN } from "./ui/useHoverMenu";
import { Icon } from "./ui/Icon";
import "./workspaceQuickSettings.css";

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), [href], [tabindex="0"]';

/** Quick controls for this workspace window, alongside the app-wide actions. */
export function WorkspaceQuickSettings({ running, open: controlledOpen, onOpenChange, hoverOpen = false }: {
  running: boolean;
  hoverOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [localOpen, setLocalOpen] = useState(false);
  const open = controlledOpen ?? localOpen;
  const openRef = useRef(open);
  openRef.current = open;
  const gear = useRef<HTMLButtonElement>(null);
  const openedByHover = useRef(false);
  const cancelHover = useRef(() => {});
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  const [position, setPosition] = useState<CSSProperties>({ visibility: "hidden" });
  const [prefs, setPrefs] = useState<OverlayPrefs | null>(null);
  const [stealth, setStealth] = useState<boolean | null>(null);
  const [stealthKey, setStealthKey] = useState("");
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [quitting, setQuitting] = useState(false);
  const prefsRequest = useRef(0);
  const settingsRequest = useRef(0);
  const opacityTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const opacityPending = useRef<number | null>(null);

  const changeOpen = useCallback((next: boolean, returnFocus = false, fromHover = false) => {
    cancelHover.current();
    if (next && !openRef.current) document.dispatchEvent(new CustomEvent(WORKSPACE_MENU_OPEN, { detail: { id } }));
    openedByHover.current = next && fromHover;
    setLocalOpen(next);
    onOpenChange?.(next);
    if (!next && returnFocus) gear.current?.focus({ preventScroll: true });
  }, [onOpenChange, id]);
  const hover = useHoverMenu({
    onOpen: () => { if (!openRef.current) changeOpen(true, false, true); },
    onClose: () => { if (openedByHover.current) changeOpen(false); },
    keepOpen: () => panel.current?.contains(document.activeElement) ?? false,
  });
  cancelHover.current = hover.cancel;
  useEffect(() => {
    const otherOpened = (event: Event) => {
      if ((event as CustomEvent<{ id: string }>).detail.id !== id) changeOpen(false);
    };
    document.addEventListener(WORKSPACE_MENU_OPEN, otherOpened);
    return () => document.removeEventListener(WORKSPACE_MENU_OPEN, otherOpened);
  }, [id, changeOpen]);

  useLayoutEffect(() => { if (open && hoverOpen) openedByHover.current = true; }, [open, hoverOpen]);

  const loadPrefs = useCallback(async () => {
    const request = ++prefsRequest.current;
    try {
      const next = await api.windowPrefs();
      if (openRef.current && request === prefsRequest.current) {
        setPrefs(next);
        setPrefsError(null);
      }
    } catch (error) {
      if (openRef.current && request === prefsRequest.current) setPrefsError(`Could not load window settings: ${String(error)}`);
    }
  }, []);
  const loadSettings = useCallback(async () => {
    const request = ++settingsRequest.current;
    try {
      const next = await api.getSettings();
      if (openRef.current && request === settingsRequest.current) {
        setStealth(next.settings.stealth);
        setStealthKey(next.settings.toggle_stealth_hotkey ?? "");
        setSettingsError(null);
      }
    } catch (error) {
      if (openRef.current && request === settingsRequest.current) setSettingsError(`Could not load app settings: ${String(error)}`);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setPrefs(null);
    setStealth(null);
    setPrefsError(null);
    setSettingsError(null);
    setActionError(null);
    void loadPrefs();
    void loadSettings();
    // Native hotkeys can update these preferences while the menu stays open.
    const refresh = () => { void loadPrefs(); void loadSettings(); };
    window.addEventListener("focus", refresh);
    let disposed = false;
    const unlisteners: Array<() => void> = [];
    const remember = (off: () => void) => { if (disposed) off(); else unlisteners.push(off); };
    void onWorkspaceWindowPrefs(next => {
      prefsRequest.current++;
      setPrefs(next);
      setPrefsError(null);
    }).then(remember).catch(() => undefined);
    void onSettingsChanged(() => void loadSettings()).then(remember).catch(() => undefined);
    return () => {
      disposed = true;
      unlisteners.forEach(off => off());
      window.removeEventListener("focus", refresh);
      prefsRequest.current++;
      settingsRequest.current++;
    };
  }, [open, loadPrefs, loadSettings]);

  useLayoutEffect(() => {
    if (!open) return;
    const fit = () => {
      if (!gear.current || !panel.current) return;
      const anchor = gear.current.getBoundingClientRect();
      const edge = 8, gap = 6;
      const width = Math.min(292, Math.max(0, window.innerWidth - edge * 2));
      const below = window.innerHeight - anchor.bottom - gap - edge;
      const above = anchor.top - gap - edge;
      const up = below < Math.min(360, panel.current.scrollHeight) && above > below;
      const maxHeight = Math.max(0, up ? above : below);
      const height = Math.min(panel.current.scrollHeight + 2, maxHeight);
      const next = { width, maxHeight, left: Math.max(edge, Math.min(anchor.right - width, window.innerWidth - width - edge)), top: up ? Math.max(edge, anchor.top - gap - height) : anchor.bottom + gap };
      setPosition(previous => Object.keys(next).every(key => previous[key as keyof CSSProperties] === next[key as keyof typeof next]) ? previous : next);
    };
    fit();
    const observer = new ResizeObserver(fit);
    if (gear.current) observer.observe(gear.current);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", fit);
    window.addEventListener("scroll", fit, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); window.removeEventListener("scroll", fit, true); };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (!openedByHover.current) panel.current?.focus({ preventScroll: true });
    const outside = (event: Event) => {
      if (!panel.current?.contains(event.target as Node) && !gear.current?.contains(event.target as Node)) changeOpen(false);
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") changeOpen(false, panel.current?.contains(document.activeElement) || document.activeElement === gear.current);
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    return () => { document.removeEventListener("keydown", escape); document.removeEventListener("pointerdown", outside); document.removeEventListener("focusin", outside); };
  }, [open, changeOpen]);
  useEffect(() => {
    if (open && !openedByHover.current && prefs && document.activeElement === panel.current) panel.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, [open, prefs]);

  const run = async (name: string, action: () => Promise<unknown>) => {
    setSaving(name);
    setActionError(null);
    try { await action(); }
    catch (error) { setActionError(String(error)); }
    finally { setSaving(null); }
  };
  const flushOpacity = () => {
    if (opacityTimer.current) clearTimeout(opacityTimer.current);
    opacityTimer.current = null;
    const next = opacityPending.current;
    opacityPending.current = null;
    if (next === null) return;
    void run("transparency", async () => {
      try { await api.setWindowOpacity(next); }
      finally { await loadPrefs(); }
    });
  };
  useEffect(() => () => {
    // Preserve the last slider value even if the parent changes pages immediately.
    if (opacityTimer.current) clearTimeout(opacityTimer.current);
    if (opacityPending.current !== null) void api.setWindowOpacity(opacityPending.current).catch(() => undefined);
  }, []);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); changeOpen(false, true); }
    if (event.key !== "Tab") return;
    const focusable = [...(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
      event.preventDefault();
      changeOpen(false, true);
    } else if (!event.shiftKey && document.activeElement === last) {
      // The portal is outside document order: continue from the gear's next control.
      event.preventDefault();
      const outside = [...document.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(element => !panel.current?.contains(element) && !element.closest("[hidden]") && element.getClientRects().length > 0);
      const next = outside[outside.indexOf(gear.current!) + 1];
      changeOpen(false, !next);
      next?.focus();
    }
  };
  const retry = () => {
    // The retry button disappears while loading; keep focus inside the popover.
    panel.current?.focus({ preventScroll: true });
    setPrefsError(null);
    setSettingsError(null);
    void loadPrefs();
    void loadSettings();
  };
  const busy = saving !== null;
  const transparency = prefs ? Math.round((1 - prefs.opacity) * 100) : 0;

  return <>
    <IconButton ref={gear} label="Quick settings" className="workspace-quick-settings-trigger" icon={<Icon name="settings" size={16} />} size="sm"
      aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} onClick={() => {
        if (openedByHover.current) { changeOpen(true); panel.current?.focus({ preventScroll: true }); }
        else changeOpen(!open);
      }} />
    {open && createPortal(<div ref={panel} id={id} role="dialog" aria-label="Quick settings" tabIndex={-1} className="workspace-quick-settings" style={position} onKeyDown={onKeyDown}
      onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} onPointerDown={() => { openedByHover.current = false; }}>
      <section aria-labelledby={`${id}-window`} className="workspace-quick-settings-group">
        <h2 id={`${id}-window`}>This window</h2>
        {!prefs && !prefsError && <p role="status">Loading window settings…</p>}
        {prefs && <>
          <div className="workspace-quick-settings-slider">
            <label htmlFor={`${id}-transparency`}>Transparency <output aria-hidden="true">{transparency}%</output></label>
            <input id={`${id}-transparency`} type="range" min={0} max={70} step={5} value={transparency} disabled={busy} aria-valuetext={`${transparency}% transparent`}
              aria-describedby={`${id}-transparency-hint`} onChange={event => {
                const opacity = 1 - Number(event.target.value) / 100;
                setPrefs(current => current && { ...current, opacity });
                opacityPending.current = opacity;
                if (opacityTimer.current) clearTimeout(opacityTimer.current);
                opacityTimer.current = setTimeout(flushOpacity, 160);
              }} onPointerUp={flushOpacity} onBlur={flushOpacity} />
            <p id={`${id}-transparency-hint`}>The background fades; text stays solid.</p>
          </div>
          <Switch label="Keep on top" checked={prefs.on_top} disabled={busy} onChange={on => void run("on-top", async () => { await api.setWindowOnTop(on); await loadPrefs(); })} />
          <Switch label="Click-through" checked={prefs.click_through} disabled={busy}
            hint={<>Clicks pass through this window. {prefs.hotkeys.click_through_off && <>{prettyKey(prefs.hotkeys.click_through_off)} turns it off.</>}</>}
            onChange={on => void run("click-through", async () => { await api.setWindowClickThrough(on); await loadPrefs(); })} />
        </>}
        {prefsError && <p role="alert" className="workspace-quick-settings-error">{prefsError}</p>}
      </section>
      <section className="workspace-quick-settings-group" aria-label="App settings">
        {stealth === null && !settingsError ? <p role="status">Loading app settings…</p> : <Switch label={isMac() ? "Stealth mode (experimental)" : "Stealth mode"}
          checked={stealth ?? false} disabled={stealth === null || busy} hint={<>{isMac() ? "Experimental capture protection for app windows." : "Exclude app windows from supported screen capture."}{stealthKey && <> {prettyKey(stealthKey)} toggles it.</>}</>}
          onChange={on => void run("stealth", async () => {
            const current = await api.getSettings();
            await api.saveSettings({ ...current.settings, stealth: on }, null);
            await loadSettings();
          })} />}
        <CaptureProtectionStatus active={open} enabled={stealth} />
        {settingsError && <p role="alert" className="workspace-quick-settings-error">{settingsError}</p>}
        {(prefsError || settingsError) && <Button size="sm" variant="ghost" onClick={retry} disabled={busy}>Retry settings</Button>}
      </section>
      {actionError && <p role="alert" className="workspace-quick-settings-error">{actionError}</p>}
      <div className="workspace-quick-settings-actions">
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run("settings", async () => { await api.openMainSettings(); changeOpen(false, true); })}>All settings…</Button>
        <Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => {
          if (running) { changeOpen(false, true); setQuitting(true); }
          else void run("quit", () => api.quitApp());
        }}>Quit Career Pilot</Button>
      </div>
    </div>, document.body)}
    <ConfirmDialog open={quitting} onOpenChange={setQuitting} title="Quit during a call?" confirmLabel="Quit Career Pilot" onConfirm={() => api.quitApp()}>
      The call stops here. It can be opened again from History while it is still running.
    </ConfirmDialog>
  </>;
}
