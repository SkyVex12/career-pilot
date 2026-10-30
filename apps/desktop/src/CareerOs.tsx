// The Career-OS link (ARCHITECTURE.md, "Career-OS link"): the settings item that connects with a
// one-time code made in Career-OS, and the dialog that searches Career-OS and pulls one
// application on demand. Nothing syncs by itself.
import { useContext, useEffect, useState } from "react";
import { rest, type ApplicationDetail, type CareerOsConnection, type CareerOsItem, type Stage } from "./api";
import { ProfileContext } from "./profiles";
import { STAGES, pullState, stageLabel } from "./pairing";
import { Chip, when } from "./shared";

export function CareerOsSettings({ ready }: { ready: boolean }) {
  const [conn, setConn] = useState<CareerOsConnection | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // Settings mounts this per account (keyed), so a link shown is always the signed-in account's.
  useEffect(() => {
    setConn(null);
    setMessage(null);
    if (ready) rest.careerOs().then(setConn, (e) => setMessage(String(e)));
  }, [ready]);
  if (!ready) return null;
  if (!conn) {
    // The link could not be loaded: say why instead of showing nothing (or an older answer).
    return message ? (
      <>
        <h3>Career-OS</h3>
        <p className="note warn">{message}</p>
      </>
    ) : null;
  }
  if (!conn.available) return null;

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
                {conn.account_type && conn.account_type !== "user" ? " (admin: their users' applications)" : ""}
                {conn.connected_at ? `, since ${when(conn.connected_at)}` : ""}
              </span>
            </div>
          ) : null}
          {conn.connected ? (
            <div className="actions">
              <button
                className="small"
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
                Sign in to Career-OS, open your profile, and make a Career Pilot code (valid 10 minutes). Career Pilot can then read your own
                applications (or, for an admin, those of your users), their job descriptions and resumes, read-only, and only when you pull one.
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
  const { here } = useContext(ProfileContext);
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<Stage | "">("");
  const [items, setItems] = useState<CareerOsItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // Results of more than one Career-OS user ("Me" on an admin link): say whose each one is.
  const several = new Set((items ?? []).map((i) => i.user?.id ?? "")).size > 1;

  const search = async () => {
    setBusy("search");
    setMessage(null);
    try {
      setItems((await rest.searchCareerOs({ q: q.trim(), stage })).items);
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
      const verb = app.pulled === "again" ? "Pulled again" : "Pulled";
      if (app.profile_id === here) {
        setMessage(`${verb}: ${app.company} · ${app.role}.`);
        onPulled(app);
      } else {
        // An application lives with its Career-OS user: switch to them to work on it.
        setMessage(`${verb} into ${item.user?.name || "their"} profile: ${app.company} · ${app.role}.`);
      }
      await search();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="subpanel">
      <div className="row">
        <h3>Pull from Career-OS</h3>
        <span className="spacer" />
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="row search-row">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="Company or role" />
        <select value={stage} onChange={(e) => setStage(e.target.value as Stage | "")}>
          <option value="">Any stage</option>
          {STAGES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <button disabled={busy !== null} onClick={() => search()}>
          {busy === "search" ? "Searching…" : "Search"}
        </button>
      </div>
      <p className="note">
        A pull copies the application, its job description and its resume (or the Career-OS base resume) into its Career-OS user&apos;s profile.
        Nothing else is imported.
      </p>
      {message && <p className="note">{message}</p>}
      {items?.length === 0 && <p className="note">No applications match.</p>}
      <div className="table-scroll">
      <table className="list">
        <tbody>
          {items?.map((i) => {
            const state = pullState(i);
            return (
              <tr key={i.id}>
                <td>
                  <div>
                    <b>{i.company}</b> · {i.role}
                  </div>
                  <div className="meta">
                    {[several ? i.user?.name || i.user?.id : null, when(i.created_at), stageLabel(i.stage), i.resume === "tailored" ? "Tailored resume" : i.resume === "base" ? "Base resume" : "No resume"]
                      .filter(Boolean)
                      .join(" · ")}
                    {state.label && (
                      <>
                        {" "}
                        <Chip state={i.pulled?.changed ? "busy" : "ok"}>{state.label}</Chip>
                      </>
                    )}
                  </div>
                </td>
                <td className="nowrap end">
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
    </div>
  );
}
