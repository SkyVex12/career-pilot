// The Career-OS link (ARCHITECTURE.md, "Career-OS link"): connecting with a one-time code made in
// Career-OS (the Call screen's setup and Settings, Account), and the dialog that searches
// Career-OS and pulls one application on demand. Nothing syncs by itself.
import { useContext, useEffect, useState } from "react";
import { rest, type ApplicationDetail, type CareerOsConnection, type CareerOsItem, type Stage } from "./api";
import { ProfileContext } from "./profiles";
import { STAGES, pullState, stageLabel } from "./pairing";
import { when } from "./shared";
import { Badge, Banner, Button, ConfirmDialog, Dialog, EmptyState, Field, Input, List, ListRow, ListState, Select, Spinner, useToast } from "./ui";

/** The connection, loaded per account (callers key it by account). */
export function useCareerOs(ready: boolean) {
  const [conn, setConn] = useState<CareerOsConnection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = async () => {
    if (!ready) return;
    try {
      setConn(await rest.careerOs());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    setConn(null);
    setError(null);
    reload();
  }, [ready]); // eslint-disable-line react-hooks/exhaustive-deps
  return { conn, setConn, error, reload };
}

/** The one-time code form. */
export function CareerOsConnect({ conn, onConnected }: { conn: CareerOsConnection; onConnected: (c: CareerOsConnection) => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const connect = async () => {
    if (code.trim().length < 6) {
      setError("Paste the whole code from Career-OS (at least 6 characters).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (conn.status === "revoked") await rest.disconnectCareerOs();
      const out = await rest.connectCareerOs(code.trim());
      setCode("");
      onConnected(out);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="ui-stack-tight"
      onSubmit={(e) => {
        e.preventDefault();
        connect();
      }}
    >
      {conn.status === "revoked" && <Banner tone="warning">Career-OS no longer accepts the link. Make a new code to connect again.</Banner>}
      <div className="career-code">
        <Field
          label="One-time code"
          error={error}
          hint="In Career-OS, open your profile and make a Career Pilot code (valid 10 minutes). Career Pilot can then read your applications (an admin: their users'), their job descriptions and resumes, read-only, and only when you pull one."
        >
          <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="Paste the code" autoComplete="off" spellCheck={false} />
        </Field>
        <Button type="submit" variant="primary" loading={busy}>
          Connect
        </Button>
      </div>
    </form>
  );
}

/** Settings, Account: the link's state, connect or disconnect. */
export function CareerOsSettings({ ready }: { ready: boolean }) {
  const { conn, setConn, error, reload } = useCareerOs(ready);
  const [disconnecting, setDisconnecting] = useState(false);
  const toast = useToast();
  if (!ready) return null;
  if (error && !conn) {
    return (
      <Banner tone="danger" title="Could not load the Career-OS link" action={<Button size="sm" onClick={reload}>Retry</Button>}>
        {error}
      </Banner>
    );
  }
  if (!conn) return <Spinner label="Loading the Career-OS link" />;
  if (!conn.available) return <p className="ui-field-hint">Career-OS is not set up on this server.</p>;
  if (!conn.connected) {
    return (
      <CareerOsConnect
        conn={conn}
        onConnected={(c) => {
          setConn(c);
          toast({ tone: "success", text: "Connected. Pull applications from Applications." });
        }}
      />
    );
  }
  return (
    <div className="ui-row-inline career-connected">
      <Badge tone="success">Connected</Badge>
      <span className="ui-text-2">
        as {conn.user?.name || conn.user?.id}
        {conn.account_type && conn.account_type !== "user" ? " (admin: their users' applications)" : ""}
        {conn.connected_at ? `, since ${when(conn.connected_at)}` : ""}
      </span>
      <Button size="sm" variant="danger-ghost" onClick={() => setDisconnecting(true)}>
        Disconnect…
      </Button>
      <ConfirmDialog
        open={disconnecting}
        onOpenChange={setDisconnecting}
        title="Disconnect Career-OS?"
        confirmLabel="Disconnect"
        onConfirm={async () => {
          await rest.disconnectCareerOs();
          setConn(await rest.careerOs());
          toast({ tone: "success", text: "Disconnected; the link is revoked in Career-OS too." });
        }}
      >
        Applications already pulled stay. To pull more you will need a new code{conn.required ? ", and the app waits until you connect again" : ""}.
      </ConfirmDialog>
    </div>
  );
}

/** Search Career-OS and pull one application, its JD and resume. */
export function PullDialog({ open, onOpenChange, onPulled }: { open: boolean; onOpenChange: (o: boolean) => void; onPulled: (app: ApplicationDetail) => void }) {
  const { here } = useContext(ProfileContext);
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<Stage | "">("");
  const [items, setItems] = useState<CareerOsItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // Results of more than one Career-OS user ("Me" on an admin link): say whose each one is.
  const several = new Set((items ?? []).map((i) => i.user?.id ?? "")).size > 1;

  const search = async () => {
    setBusy("search");
    setError(null);
    try {
      setItems((await rest.searchCareerOs({ q: q.trim(), stage })).items);
    } catch (e) {
      setError(String(e));
      setItems([]);
    } finally {
      setBusy(null);
    }
  };
  useEffect(() => {
    if (open) {
      setItems(null);
      setMessage(null);
      search();
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

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
        setMessage(`${verb} into ${item.user?.name || "their"} profile: ${app.company} · ${app.role}. Switch profile to work on it.`);
      }
      await search();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="Pull from Career-OS"
      description="Copies the application, its job description and its resume into its Career-OS user's profile. Nothing else is imported, and nothing syncs by itself."
      footer={<Button onClick={() => onOpenChange(false)}>Done</Button>}
    >
      <form
        className="pull-search"
        onSubmit={(e) => {
          e.preventDefault();
          search();
        }}
      >
        <Input aria-label="Company or role" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Company or role" />
        <Select aria-label="Stage" value={stage} onChange={(e) => setStage(e.target.value as Stage | "")}>
          <option value="">Any stage</option>
          {STAGES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </Select>
        <Button type="submit" loading={busy === "search"}>
          Search
        </Button>
      </form>
      {message && <Banner tone="success">{message}</Banner>}
      <ListState
        items={busy === "search" && !items ? null : items}
        error={error}
        onRetry={search}
        loadingLabel="Career-OS applications"
        empty={<EmptyState title="No applications match">Try another company or role, or any stage.</EmptyState>}
      >
        {(list) => (
          <List label="Career-OS applications">
            {list.map((i) => {
              const state = pullState(i);
              const title = `${i.company} · ${i.role}`;
              return (
                <ListRow
                  key={i.id}
                  title={title}
                  fullTitle={title}
                  meta={[several ? i.user?.name || i.user?.id : null, when(i.created_at), stageLabel(i.stage), i.resume === "tailored" ? "Tailored resume" : i.resume === "base" ? "Base resume" : "No resume"]
                    .filter(Boolean)
                    .join(" · ")}
                  trailing={
                    <>
                      {state.label && <Badge tone={i.pulled?.changed ? "warning" : "success"}>{i.pulled?.changed ? "Changed since the pull" : "Pulled"}</Badge>}
                      {state.action && (
                        <Button size="sm" variant={state.action === "Pull" ? "primary" : "secondary"} disabled={busy !== null && busy !== i.id} loading={busy === i.id} onClick={() => pull(i)} aria-label={`${state.action}: ${title}`}>
                          {state.action}
                        </Button>
                      )}
                    </>
                  }
                />
              );
            })}
          </List>
        )}
      </ListState>
    </Dialog>
  );
}
