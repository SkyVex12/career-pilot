// History: calls still running (open one here, or end it), past calls (open one, delete one)
// and live minutes this period. The design system's reference screen
// (docs/design/ui-proposal.md, "History"). Account deletion is in Settings (DeleteAccount).
import { useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { Mode } from "@copilot/protocol";
import { api, rest, type LiveSession, type SessionItem, type Usage } from "./api";
import { useOnShow, when } from "./shared";
import { SessionDetail } from "./SessionDetail";
import { ProfileContext } from "./profiles";
import { Badge, Banner, Button, ConfirmDialog, EmptyState, List, ListRow, ListState, PageHeader, SegmentedControl, type Tone } from "./ui";

type Filter = "all" | Mode;
const FILTERS = [
  ["all", "All"],
  ["interview", "Interviews"],
  ["standup", "Standups"],
  ["meeting", "Meetings"],
] as const;

export const MODE_LABEL: Record<string, string> = { interview: "Interview", standup: "Standup", meeting: "Meeting" };

export function statusBadge(status: SessionItem["status"]): { tone: Tone; text: string } {
  if (status === "ready") return { tone: "success", text: "Ready" };
  if (status === "live") return { tone: "info", text: "Live" };
  return { tone: "warning", text: "Processing" };
}

/** "Interview · Northwind technical", "Standup · Fernhill Logistics". */
export function sessionTitle(s: Pick<SessionItem, "mode" | "title" | "employer_id">, companies: Record<string, string>): string {
  const company = s.employer_id ? companies[s.employer_id] : undefined;
  return [MODE_LABEL[s.mode] ?? s.mode, company, s.title].filter(Boolean).join(" · ");
}

export function minutes(s: Pick<SessionItem, "ended_at" | "billed_seconds">): string {
  return s.ended_at ? `${Math.max(1, Math.round(s.billed_seconds / 60))} min` : "";
}

export function History({
  running,
  liveId,
  onOpenLive,
  active = true,
  ready = true,
  onSignIn,
}: {
  /** Shown now: a kept-open tab refreshes when shown again and polls only while shown. */
  active?: boolean;
  /** Signed in (or the dev token set): the API can be called. */
  ready?: boolean;
  /** A call runs in this app now. */
  running: boolean;
  /** That call's id. */
  liveId: string | null;
  onOpenLive: (live: LiveSession) => Promise<void>;
  onSignIn?: () => void;
}) {
  const [sessions, setSessions] = useState<SessionItem[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [companies, setCompanies] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    if (!ready) return;
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
  }, [ready]);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(active, load);

  const more = async () => {
    if (!next) return;
    setLoadingMore(true);
    try {
      const s = await rest.sessions(next);
      setSessions((cur) => [...(cur ?? []), ...s.sessions]);
      setNext(s.next_before);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const shown = useMemo(() => (sessions === null ? null : sessions.filter((s) => filter === "all" || s.mode === filter)), [sessions, filter]);

  if (!ready) {
    return (
      <>
        <PageHeader title="History" />
        <EmptyState icon="↺" title="Sign in to see your calls" action={onSignIn && <Button variant="primary" onClick={onSignIn}>Open settings</Button>}>
          Past calls, their summaries and replays appear here once you are signed in.
        </EmptyState>
      </>
    );
  }

  if (open) {
    return (
      <SessionDetail
        sessionId={open}
        companies={companies}
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
      <PageHeader
        title="History"
        meta={
          usage ? (
            <span className="ui-num">
              {usage.live_minutes} live minutes in {usage.period} · {usage.sessions} {usage.sessions === 1 ? "call" : "calls"}
            </span>
          ) : (
            <span aria-hidden="true">&nbsp;</span>
          )
        }
      />
      {active && <LiveNow running={running} liveId={liveId} onOpen={onOpenLive} onEnded={load} />}
      <div className="history-filters">
        <SegmentedControl label="Show" size="sm" value={filter} options={FILTERS} onChange={setFilter} />
      </div>
      <ListState
        items={shown}
        error={error}
        onRetry={load}
        loadingLabel="calls"
        empty={
          <EmptyState icon="↺" title={filter === "all" ? "No calls yet" : `No ${FILTERS.find(([f]) => f === filter)?.[1].toLowerCase()} yet`}>
            {filter === "all" ? "Each call you start appears here with its summary, replay and transcript." : "Calls of other kinds are under All."}
          </EmptyState>
        }
      >
        {(items) => (
          <List label="Past calls">
            {items.map((s) => {
              const b = statusBadge(s.status);
              const title = sessionTitle(s, companies);
              return (
                <ListRow
                  key={s.id}
                  onClick={() => setOpen(s.id)}
                  leading={<span className="history-when">{s.started_at ? when(s.started_at) : "–"}</span>}
                  title={title}
                  fullTitle={title}
                  trailing={
                    <>
                      <span className="ui-num">{minutes(s)}</span>
                      <Badge tone={b.tone}>{b.text}</Badge>
                    </>
                  }
                />
              );
            })}
          </List>
        )}
      </ListState>
      {error && sessions && sessions.length > 0 && (
        <p className="ui-field-error" role="alert">
          {error}
        </p>
      )}
      {next && filter === "all" && (
        <div className="history-more">
          <Button onClick={more} loading={loadingMore}>
            Load older calls
          </Button>
        </div>
      )}
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
  const [ending, setEnding] = useState<LiveSession | null>(null);

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

  if (live.length === 0) return message ? <Banner tone="success">{message}</Banner> : null;

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
    await api.endLiveSession(l.id, current);
    setMessage("Ended. Its summary appears in History in a minute.");
    await load();
    onEnded?.();
  };

  return (
    <section className="live-now" aria-label="Live now">
      {live.map((l) => {
        const here = l.id === liveId;
        const title = [MODE_LABEL[l.mode] ?? l.mode, l.title].filter(Boolean).join(" · ");
        return (
          <Banner
            key={l.id}
            tone="info"
            title={`Live now: ${title}`}
            action={
              <div className="ui-row-inline">
                {!here && (
                  <Button size="sm" disabled={running || busy !== null} loading={busy === l.id} onClick={() => open(l)} title={running ? "End the call running in this app first" : undefined}>
                    Open here
                  </Button>
                )}
                <Button size="sm" variant="danger-ghost" disabled={busy !== null} onClick={() => setEnding(l)}>
                  End…
                </Button>
              </div>
            }
          >
            {here ? "Running in this app." : l.connected ? "Running on another computer." : "Waiting for an app to reconnect."}
            {l.started_at ? ` Started ${when(l.started_at)}.` : ""}
            {running && !here ? " End the call running here to open it." : ""}
          </Banner>
        );
      })}
      {message && <p className="ui-field-hint">{message}</p>}
      <ConfirmDialog
        open={ending !== null}
        onOpenChange={(o) => !o && setEnding(null)}
        title="End this call?"
        confirmLabel="End call"
        onConfirm={() => ending && end(ending)}
      >
        It ends wherever it runs, and its summary is written as usual.
      </ConfirmDialog>
    </section>
  );
}

/** In Settings: the whole account, behind a typed confirmation. */
export function DeleteAccount() {
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  return (
    <div className="ui-card account-danger">
      <div className="ui-card-body ui-row-inline account-danger-row">
        <div className="ui-stack-tight">
          <h3 className="ui-card-title">Delete account</h3>
          <p className="ui-field-hint">Deletes every call, transcript, document and memory fact, and your encryption keys. This cannot be undone.</p>
        </div>
        <Button variant="danger-ghost" onClick={() => setConfirming(true)}>
          Delete my account…
        </Button>
      </div>
      {message && (
        <div className="ui-card-body">
          <Banner tone="info">{message}</Banner>
        </div>
      )}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Delete your account?"
        typed="delete"
        confirmLabel="Delete everything"
        onConfirm={async () => {
          await api.deleteAccount();
          setMessage("Your account is being deleted: calls, documents, memory and keys. Local recordings are gone.");
        }}
      >
        Every call, transcript, document and memory fact is deleted, with your encryption keys. This cannot be undone.
      </ConfirmDialog>
    </div>
  );
}
