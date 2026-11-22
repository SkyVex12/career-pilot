import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, onServer, type ServerState, type UpdateState } from "./api";
import { bar } from "./updates";
import { Button, IconButton } from "./ui";
import { Icon } from "./ui/Icon";

/** Persistent system notices share the toast stack, outside page layout and scrolling. */
function Notification({ title, children, tone = "info", actions, onDismiss }: {
  title: string; children: ReactNode; tone?: "info" | "success" | "danger";
  actions?: ReactNode; onDismiss: () => void;
}) {
  return <section className={`app-notification app-notification-${tone}`} aria-label={title}>
    <div className="app-notification-head"><span className="app-notification-mark"><Icon name={tone === "success" ? "check" : tone === "danger" ? "shield" : "call"} size={18} /></span><strong>{title}</strong><IconButton label={`Dismiss ${title.toLowerCase()} notification`} size="sm" icon={<Icon name="close" size={15} />} onClick={onDismiss} /></div>
    <div className="app-notification-body" role={tone === "danger" ? "alert" : "status"}>{children}</div>
    {actions && <div className="app-notification-actions">{actions}</div>}
  </section>;
}

export function ServerNotification() {
  const [server, setServer] = useState<ServerState | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const revision = useRef(0);
  useEffect(() => {
    let alive = true;
    const off = onServer(next => { if (alive) { revision.current++; setServer(next); if (next.state === "ready") setDismissed(null); } });
    return () => { alive = false; revision.current++; void off.then(stop => stop()); };
  }, []);
  if (!server || server.state === "ready" || dismissed === server.state) return null;
  const retry = () => {
    const ticket = ++revision.current;
    setServer({ state: "starting", detail: null }); setDismissed(null);
    void api.wakeServer().catch(error => { if (ticket === revision.current) setServer({ state: "failed", detail: String(error) }); });
  };
  return <Notification title={server.state === "starting" ? "Starting the server" : "Server unavailable"} tone={server.state === "starting" ? "info" : "danger"} onDismiss={() => setDismissed(server.state)} actions={server.state === "starting" ? undefined : <Button size="sm" onClick={retry}>Try again</Button>}>
    <p>{server.state === "starting" ? "This usually takes a minute or two." : "We couldn’t connect to your server."}</p>
    {server.detail && <p className="app-notification-detail">{server.detail}</p>}
    {server.state === "starting" && <span className="update-track moving" role="progressbar" aria-label="Starting server"><span className="update-fill" /></span>}
  </Notification>;
}

export function UpdateNotification({ update, running }: { update: UpdateState; running: boolean }) {
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const key = `${update.state}/${"version" in update ? update.version : ""}`;
  useEffect(() => { setError(null); setDismissed(null); }, [key]);
  const status = bar(update, running);
  if (status.kind === "none" || dismissed === key) return null;
  return <Notification title="App update" tone={status.tone === "error" ? "danger" : status.tone === "done" ? "success" : "info"} onDismiss={() => setDismissed(key)} actions={status.action && <Button size="sm" variant="primary" disabled={!status.action.enabled} onClick={() => { setError(null); void api.installUpdate().catch(e => setError(String(e))); }}>{status.action.label}</Button>}>
    <p>{status.text}</p>
    {error && <p className="ui-field-error">{error}</p>}
    {status.progress !== undefined && <span className={`update-track${status.progress === null ? " moving" : ""}`} role="progressbar" aria-label="App update progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={status.progress ?? undefined}><span className="update-fill" style={status.progress === null ? undefined : { width: `${status.progress}%` }} /></span>}
  </Notification>;
}
