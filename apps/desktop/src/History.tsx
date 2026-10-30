// History: calls still running (open one here, or end it), past calls (open one, delete one)
// and live minutes this period. The design system's reference screen
// (docs/design/ui-proposal.md, "History"). Account deletion is in Settings.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Mode } from "@copilot/protocol";
import { rest, type LiveSession, type SessionItem, type Usage } from "./api";
import { useOnShow, when } from "./shared";
import { SessionDetail } from "./SessionDetail";
import { LiveNow, minutes, sessionTitle, statusBadge } from "./calls";
import { Badge, Button, EmptyState, List, ListRow, ListState, PageHeader, SegmentedControl } from "./ui";

export { MODE_LABEL, minutes, sessionTitle, statusBadge } from "./calls";

type Filter = "all" | Mode;
const FILTERS = [
  ["all", "All"],
  ["interview", "Interviews"],
  ["standup", "Standups"],
  ["meeting", "Meetings"],
] as const;

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
