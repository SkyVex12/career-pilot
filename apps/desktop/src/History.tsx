// History: calls still running (open one here, or end it), past sessions (open one, delete
// one), live minutes this period, and account deletion behind a confirmation.
import { useCallback, useContext, useEffect, useState } from "react";
import { api, rest, type LiveSession, type SessionItem, type Usage } from "./api";
import { Chip, when } from "./ui";
import { SessionDetail } from "./SessionDetail";
import { ProfileContext } from "./profiles";

export function History({
  running,
  liveId,
  onOpenLive,
}: {
  /** A call runs in this app now. */
  running: boolean;
  /** That call's id. */
  liveId: string | null;
  onOpenLive: (live: LiveSession) => Promise<void>;
}) {
  const [sessions, setSessions] = useState<SessionItem[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [companies, setCompanies] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, u, e] = await Promise.all([rest.sessions(), rest.usage(), rest.employers().catch(() => ({ employers: [] }))]);
      setCompanies(Object.fromEntries(e.employers.map((x) => [x.id, x.name])));
      setSessions(s.sessions);
      setNext(s.next_before);
      setUsage(u);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const more = async () => {
    if (!next) return;
    try {
      const s = await rest.sessions(next);
      setSessions((cur) => [...(cur ?? []), ...s.sessions]);
      setNext(s.next_before);
    } catch (e) {
      setError(String(e));
    }
  };

  if (open) {
    return (
      <SessionDetail
        sessionId={open}
        onClose={() => setOpen(null)}
        onDeleted={() => {
          setOpen(null);
          load();
        }}
      />
    );
  }

  return (
    <>
      <LiveNow running={running} liveId={liveId} onOpen={onOpenLive} onEnded={load} />
      <section className="panel">
        <div className="usage">
          <div>
            <div className="big">{usage ? usage.live_minutes : "–"}</div>
            <div className="muted">live minutes in {usage?.period ?? "this period"}</div>
          </div>
          <div>
            <div className="big">{usage ? usage.sessions : "–"}</div>
            <div className="muted">sessions</div>
          </div>
        </div>
        {error && <p className="note warn">{error}</p>}
        <h3>Sessions</h3>
        {sessions?.length === 0 && <p className="note">No sessions yet.</p>}
        <table className="list clickable">
          <tbody>
            {sessions?.map((s) => (
              <tr key={s.id} onClick={() => setOpen(s.id)}>
                <td className="nowrap">{s.started_at ? when(s.started_at) : "–"}</td>
                <td>
                  {s.mode}
                  {s.employer_id && companies[s.employer_id] ? ` at ${companies[s.employer_id]}` : ""}
                  {s.title ? `: ${s.title}` : ""}
                </td>
                <td className="muted">{s.ended_at ? `${Math.max(1, Math.round(s.billed_seconds / 60))} min` : ""}</td>
                <td>
                  <Chip state={s.status === "ready" ? "ok" : s.status === "live" ? "busy" : "off"}>{s.status}</Chip>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {next && (
          <button className="link" onClick={more}>
            Older sessions
          </button>
        )}
      </section>
      <DeleteAccount />
    </>
  );
}

/** The profile's call still running on the server: after the app quit or crashed mid-call, or
 * on another computer. Open takes the call over here (this computer's audio from now on); End
 * ends it wherever it runs. Each profile sees its own. */
export function LiveNow({
  running,
  liveId,
  onOpen,
  onEnded,
}: {
  running: boolean;
  liveId: string | null;
  onOpen: (live: LiveSession) => Promise<void>;
  onEnded?: () => void;
}) {
  const { current } = useContext(ProfileContext);
  const [live, setLive] = useState<LiveSession[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLive((await rest.liveSessions()).sessions);
    } catch {
      setLive([]); // an older server: no list
    }
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 10_000);
    return () => window.clearInterval(t);
  }, [load]);

  if (live.length === 0) return null;

  const open = async (l: LiveSession) => {
    setBusy(l.id);
    setMessage(null);
    try {
      await onOpen(l);
    } catch (e) {
      setMessage(String(e));
      setBusy(null);
    }
  };
  const end = async (l: LiveSession) => {
    if (!window.confirm(`End this ${l.mode}? Its summary is written as usual.`)) return;
    setBusy(l.id);
    setMessage(null);
    try {
      await api.endLiveSession(l.id, current);
      setMessage("Ended. The summary appears below in a minute.");
      await load();
      onEnded?.();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel">
      <h3>Live now</h3>
      <table className="list">
        <tbody>
          {live.map((l) => {
            const here = l.id === liveId;
            return (
              <tr key={l.id}>
                <td className="nowrap">{l.started_at ? when(l.started_at) : "–"}</td>
                <td>
                  {l.mode}
                  {l.title ? `: ${l.title}` : ""}
                </td>
                <td>
                  <Chip state={here || l.connected ? "busy" : "off"}>{here ? "in this app" : l.connected ? "connected" : "waiting"}</Chip>
                </td>
                <td className="actions nowrap">
                  {!here && (
                    <button
                      disabled={running || busy !== null}
                      title={running ? "End the call running in this app first" : "Continue this call here, with this computer's audio"}
                      onClick={() => open(l)}
                    >
                      Open
                    </button>
                  )}
                  <button className="danger" disabled={busy !== null} onClick={() => end(l)}>
                    End
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="note">
        This profile's call is still running, here or on another computer. Open continues it in this app; the app running it stops. End ends it and writes its summary.
      </p>
      {message && <p className="note warn">{message}</p>}
    </section>
  );
}

function DeleteAccount() {
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const remove = async () => {
    try {
      await api.deleteAccount();
      setMessage("Your account is being deleted: sessions, documents, memory and keys. Local recordings are gone.");
      setConfirming(false);
    } catch (e) {
      setMessage(String(e));
    }
  };
  return (
    <section className="panel danger-zone">
      <h3>Delete account</h3>
      <p className="note">Deletes every session, transcript, document and memory fact, and your encryption keys. This cannot be undone.</p>
      {!confirming ? (
        <div className="actions">
          <button className="danger" onClick={() => setConfirming(true)}>
            Delete my account…
          </button>
        </div>
      ) : (
        <div className="actions">
          <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder='Type "delete" to confirm' />
          <button className="danger" disabled={typed.trim().toLowerCase() !== "delete"} onClick={remove}>
            Delete everything
          </button>
          <button onClick={() => setConfirming(false)}>Cancel</button>
        </div>
      )}
      {message && <p className="note">{message}</p>}
    </section>
  );
}
