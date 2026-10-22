// History: past sessions (open one, delete one), live minutes this period, and account
// deletion behind a confirmation.
import { useCallback, useEffect, useState } from "react";
import { api, rest, type SessionItem, type Usage } from "./api";
import { Chip, when } from "./Documents";
import { SessionDetail } from "./SessionDetail";

export function History() {
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
                <td>{s.started_at ? when(s.started_at) : "–"}</td>
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
        <button className="danger" onClick={() => setConfirming(true)}>
          Delete my account…
        </button>
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
