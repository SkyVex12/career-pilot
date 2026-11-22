import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Banner, Button, SkeletonList } from "./ui";
import { Icon } from "./ui/Icon";
import { api } from "./api";
type Attachment = { attachment_id: string; session_id: string; device_name: string; revoked: boolean; expires_at: string };
export function CompanionDevices({ sessionId }: { sessionId: string | null }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Attachment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const revision = useRef(0);
  const alive = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; revision.current++; }; }, []);
  const load = useCallback(async () => {
    const ticket = ++revision.current;
    setLoading(true); setError(null);
    try {
      const result = await invoke<{ items: Attachment[] }>("api_call", { method: "GET", path: "/companion/attachments", body: null });
      if (alive.current && ticket === revision.current) setItems(result.items.filter(a => a.session_id === sessionId && !a.revoked && new Date(a.expires_at).getTime() > Date.now()));
    } catch (e) { if (alive.current && ticket === revision.current) setError(String(e)); }
    finally { if (alive.current && ticket === revision.current) setLoading(false); }
  }, [sessionId]);
  useEffect(() => {
    revision.current++; setItems(null); setError(null);
    if (open) void load();
    return () => { revision.current++; };
  }, [open, load]);
  const revoke = async (id: string) => {
    const ticket = revision.current;
    setBusy(true);
    try {
      await invoke("api_call", { method: "DELETE", path: `/companion/attachments/${id}`, body: null });
      if (alive.current && ticket === revision.current) await load();
    } catch (e) { if (alive.current && ticket === revision.current) setError(String(e)); }
    finally { if (alive.current) setBusy(false); }
  };
  return <details className="ui-disclosure companion-displays" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary><Icon name="monitor" size={18} /><span>Use your phone for guidance</span><span className="ui-disclosure-meta">Desktop audio · phone display</span></summary>
    <div className="ui-disclosure-body ui-stack">
      <p className="ui-field-hint">Open Career Pilot Mobile, sign in with this account and choose this profile. On Call, connect to the active desktop call. Keep your headphones on this computer.</p>
      <div className="ui-row-inline">
        <Button size="sm" onClick={() => void api.showOverlay(false).catch(e => setError(String(e)))}>Hide desktop overlay</Button>
        <Button size="sm" onClick={() => void api.showOverlay(true).catch(e => setError(String(e)))}>Restore desktop overlay</Button>
        <Button size="sm" loading={loading && items !== null} disabled={busy || loading} onClick={() => void load()}>Refresh displays</Button>
      </div>
      {error && <Banner tone="danger" title="Display status unavailable" action={<Button size="sm" disabled={loading || busy} onClick={() => void load()}>Retry</Button>}>{error}</Banner>}
      {loading && items === null && <SkeletonList rows={1} label="Loading authorized displays" />}
      {items?.length === 0 && !error && !loading && <p className="calendar-empty">No displays authorized yet. Connect this call from your phone to see guidance there.</p>}
      {!!items?.length && <ul className="companion-display-list">{items.map(a => <li key={a.attachment_id}><div><strong>{a.device_name}</strong><span className="ui-field-hint">Authorized until {new Date(a.expires_at).toLocaleTimeString()}</span></div><Button size="sm" disabled={busy || loading} onClick={() => void revoke(a.attachment_id)}>Disconnect display</Button></li>)}</ul>}
      <p className="ui-field-hint">The phone doesn’t use its microphone. Disconnecting a display keeps this call running. Check your screen-sharing preview before hiding the overlay.</p>
    </div>
  </details>;
}
