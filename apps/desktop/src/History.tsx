// History: calls still running (open one here, or end it), past calls (open one, delete one)
// and live minutes this period. The design system's reference screen
// (docs/design/ui-proposal.md, "History"). Account deletion is in Settings.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Mode } from "@copilot/protocol";
import { rest, type ReadPolicy, type LiveSession, type SessionItem, type Usage } from "./api";
import { useOnShow, when } from "./shared";
import { SessionDetail } from "./SessionDetail";
import { LiveNow, minutes, sessionTitle, statusBadge } from "./calls";
import { Badge, Button, EmptyState, List, ListRow, ListState, PageHeader, SegmentedControl, Input } from "./ui";
import { Icon } from "./ui/Icon";
import { usePolling } from "./usePolling";
import { AccountUsage } from "./Usage";
import "./feature-pages.css";

export { MODE_LABEL, minutes, sessionTitle, statusBadge } from "./calls";

type Filter = "all" | Mode;
const FILTERS = [
  ["all", "All"],
  ["interview", "Interviews"],
  ["standup", "Standups"],
  ["meeting", "Meetings"],
] as const;

function UsageSummary({ usage }: { usage: Usage }) {
  const measured = usage.measured_seconds ?? 0;
  const legacy = usage.legacy_estimated_seconds ?? usage.live_seconds;
  const calls = usage.measured_sessions ?? 0;
  const minutes = (seconds: number) => new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(seconds / 60);
  return <div className="history-usage" role="group" aria-label="Account usage">
    <span className="ui-num"><strong>{minutes(measured)}</strong> connected minutes in {usage.period} (UTC) · {calls} {calls === 1 ? "call" : "calls"}</span>
    {legacy > 0 && <span>Historical estimate: {minutes(legacy)} minutes · separate from recorded usage.</span>}
  </div>;
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
  const [cursor, setCursor] = useState<string | null>(null);
  const pages = useRef(1), revision = useRef(0);
  const [refreshing, setRefreshing] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [companies, setCompanies] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [loadingMore, setLoadingMore] = useState(false);
  const [query, setQuery] = useState("");

  const read = useCallback(async (policy?: ReadPolicy) => {
    if (!ready) return;
    const ticket = ++revision.current;
    setRefreshing(true);
    try {
      const [s, u, e] = await Promise.all([rest.sessions(undefined, policy), rest.usage(policy), rest.employers(policy).catch(() => ({ employers: [] }))]);
      const items = [...s.sessions]; let last = s, loaded = 1;
      while (loaded < pages.current && (last.next_cursor || last.next_before)) {
        last = await rest.sessions(last.next_before, policy, last.next_cursor);
        items.push(...last.sessions); loaded++;
      }
      if (ticket !== revision.current) return;
      pages.current = loaded;
      setCompanies(Object.fromEntries(e.employers.map((x) => [x.id, x.name])));
      setSessions([...new Map(items.map(item => [item.id, item])).values()]);
      setNext(last.next_before);
      setCursor(last.next_cursor ?? null);
      setUsage(u);
      setError(null);
    } catch (e) {
      if (ticket === revision.current) setError(String(e));
    } finally { if (ticket === revision.current) setRefreshing(false); }
  }, [ready]);
  const load = useCallback(() => read(), [read]);
  const refreshUsage = useCallback(async () => {
    const ticket = revision.current;
    try {
      const current = await rest.usage({ wake: false });
      if (ticket === revision.current) setUsage(current);
      return true;
    } catch { return false; }
  }, []);
  // Final worker checkpoints may arrive after a local call ends. Account usage
  // can also grow on another device, so keep visible idle screens up to date.
  usePolling(refreshUsage, running ? 15_000 : 30_000, active && ready, { timeoutMs: Infinity, key: liveId ?? "" });
  useEffect(() => () => { ++revision.current; }, []);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(active, load);

  const more = async () => {
    if ((!next && !cursor) || refreshing || loadingMore) return;
    const ticket = revision.current;
    setLoadingMore(true);
    try {
      const s = await rest.sessions(next, undefined, cursor);
      if (ticket !== revision.current) return;
      pages.current++;
      setSessions((cur) => [...new Map([...(cur ?? []), ...s.sessions].map(item => [item.id, item])).values()]);
      setNext(s.next_before);
      setCursor(s.next_cursor ?? null);
    } catch (e) {
      if (ticket === revision.current) setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const shown = useMemo(() => {
    const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return sessions === null ? null : sessions.filter(s => (filter === "all" || s.mode === filter) && words.every(word => `${sessionTitle(s, companies)} ${s.platform} ${s.started_at ? when(s.started_at) : ""}`.toLocaleLowerCase().includes(word)));
  }, [sessions, filter, query, companies]);

  if (!ready) {
    return (
      <>
        <PageHeader title="Past calls" />
        <EmptyState icon={<Icon name="history" size={28} />} title="Sign in to see your calls" action={onSignIn && <Button variant="primary" onClick={onSignIn}>Open settings</Button>}>
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
    <div className="history-workspace feature-page">
      <PageHeader
        title="Past calls"
        actions={<Button size="sm" loading={refreshing} disabled={loadingMore} onClick={() => void read({ refresh: true })}>Refresh</Button>}
      />
      <section className="history-overview" aria-label="Usage overview"><AccountUsage active={active && ready} running={running} />{usage && <UsageSummary usage={usage} />}</section>
      {active && <LiveNow running={running} liveId={liveId} onOpen={onOpenLive} onEnded={load} />}
      <div className="history-filters">
        <SegmentedControl label="Show" size="sm" value={filter} options={FILTERS} onChange={setFilter} />
        <Input type="search" aria-label="Search past calls" placeholder="Search calls" value={query} onChange={e => setQuery(e.target.value)} />
      </div>
      {shown && sessions && sessions.length > 0 && <div className="history-list-meta"><span>{shown.length} {shown.length === 1 ? "call" : "calls"}{query.trim() ? " found" : " shown"}</span>{(next || cursor) && <span>Search covers loaded calls</span>}</div>}
      <ListState
        items={shown}
        error={error}
        onRetry={() => read({ refresh: true })}
        loadingLabel="calls"
        empty={
          <EmptyState icon={<Icon name="history" size={28} />} title={query.trim() ? "No matching calls" : filter === "all" ? "No calls yet" : `No ${FILTERS.find(([f]) => f === filter)?.[1].toLowerCase()} yet`} action={query.trim() ? <Button size="sm" onClick={() => setQuery("")}>Clear search</Button> : filter !== "all" ? <Button size="sm" onClick={() => setFilter("all")}>Show all calls</Button> : undefined}>
            {query.trim() ? "Try a call title, company, or platform." : filter === "all" ? "Saved calls and their available summaries, transcripts and replays appear here." : "Calls of other kinds are under All."}
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
                  leading={<span className="history-session-icon"><Icon name={s.mode === "interview" ? "briefcase" : "call"} /></span>}
                  title={title}
                  meta={<span>{s.started_at ? when(s.started_at) : "Date unavailable"} · {s.mode === "interview" ? "Interview" : s.mode === "standup" ? "Standup" : "Meeting"}</span>}
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
      {(next || cursor) && (
        <div className="history-more">
          <Button onClick={more} loading={loadingMore} disabled={refreshing}>
            Load older calls
          </Button>
        </div>
      )}
    </div>
  );
}
