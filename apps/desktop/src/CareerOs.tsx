// The Career-OS link (ARCHITECTURE.md, "Career-OS link"): the settings item that connects with a
// one-time code made in Career-OS, and the dialog that searches Career-OS and pulls one
// application on demand. Nothing syncs by itself.
import { useEffect, useState } from "react";
import { rest, type ApplicationDetail, type CareerOsConnection, type CareerOsItem, type Stage } from "./api";
import { STAGES, pullState, stageLabel } from "./pairing";
import { Chip, when } from "./ui";

export function CareerOsSettings({ ready }: { ready: boolean }) {
  const [conn, setConn] = useState<CareerOsConnection | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    if (ready) rest.careerOs().then(setConn, (e) => setMessage(String(e)));
  }, [ready]);
  if (!ready || !conn || !conn.available) return null;

  const act = async (what: () => Promise<CareerOsConnection | null>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      const out = await what();
      setConn(out ?? (await rest.careerOs()));
      setMessage(done);
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3>Career-OS</h3>
      <div className="grid">
        <label>Connection</label>
        <div>
          {conn.connected ? (
            <div className="row">
              <Chip state="ok">Connected</Chip>
              <span>
                as {conn.user?.name || conn.user?.id}
                {conn.connected_at ? `, since ${when(conn.connected_at)}` : ""}
              </span>
              <button
                className="danger"
                disabled={busy}
                onClick={() => act(async () => (await rest.disconnectCareerOs(), null), "Disconnected; the link is revoked in Career-OS too.")}
              >
                Disconnect
              </button>
            </div>
          ) : (
            <>
              {conn.status === "revoked" && <p className="note warn">Career-OS no longer accepts the link. Make a new code to connect again.</p>}
              <div className="row">
                <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="One-time code from Career-OS" />
                <button
                  className="primary"
                  disabled={busy || code.trim().length < 6}
                  onClick={() =>
                    act(async () => {
                      if (conn.status === "revoked") await rest.disconnectCareerOs();
                      const out = await rest.connectCareerOs(code.trim());
                      setCode("");
                      return out;
                    }, "Connected. Pull applications from the Applications tab.")
                  }
                >
                  Connect Career-OS
                </button>
              </div>
              <p className="note">
                Sign in to Career-OS as yourself, open your profile, and make a Career Pilot code (valid 10 minutes). Career Pilot can then read
                your own applications, their job descriptions and resumes, read-only, and only when you pull one.
              </p>
            </>
          )}
          {message && <p className="note">{message}</p>}
        </div>
      </div>
    </>
  );
}

export function PullDialog({ onClose, onPulled }: { onClose: () => void; onPulled: (app: ApplicationDetail) => void }) {
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<Stage | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [items, setItems] = useState<CareerOsItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const search = async () => {
    setBusy("search");
    setMessage(null);
    try {
      setItems((await rest.searchCareerOs({ q: q.trim(), stage, from, to })).items);
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(null);
    }
  };
  useEffect(() => {
    search();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const pull = async (item: CareerOsItem) => {
    setBusy(item.id);
    setMessage(null);
    try {
      const app = await rest.pullCareerOs(item.id);
      setMessage(`${app.pulled === "again" ? "Pulled again" : "Pulled"}: ${app.company} · ${app.role}.`);
      onPulled(app);
      await search();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card">
      <div className="row">
        <h3>Pull from Career-OS</h3>
        <span className="spacer" />
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="row">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="Company or role" />
        <select value={stage} onChange={(e) => setStage(e.target.value as Stage | "")}>
          <option value="">Any stage</option>
          {STAGES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} title="Applied from" />
        <input type="date" value={to} onChange={(e) => setTo(e.target.value)} title="Applied until" />
        <button disabled={busy !== null} onClick={search}>
          {busy === "search" ? "Searching…" : "Search"}
        </button>
      </div>
      <p className="note">A pull copies the application, its job description and its resume (or your Career-OS base resume). Nothing else is imported.</p>
      {message && <p className="note">{message}</p>}
      {items?.length === 0 && <p className="note">No applications match.</p>}
      <table className="list">
        <tbody>
          {items?.map((i) => {
            const state = pullState(i);
            return (
              <tr key={i.id}>
                <td>
                  <b>{i.company}</b> · {i.role}
                </td>
                <td className="muted">{when(i.created_at)}</td>
                <td>{stageLabel(i.stage)}</td>
                <td className="muted">{i.resume === "tailored" ? "Tailored resume" : i.resume === "base" ? "Base resume" : "No resume"}</td>
                <td>{state.label && <Chip state={i.pulled?.changed ? "busy" : "ok"}>{state.label}</Chip>}</td>
                <td>
                  {state.action && (
                    <button disabled={busy !== null} onClick={() => pull(i)}>
                      {busy === i.id ? "Pulling…" : state.action}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
