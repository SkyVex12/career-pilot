// Calls in words, and the profile's calls still running elsewhere ("Live now"), shared by the
// Call screen and History.
import { useCallback, useContext, useEffect, useState } from "react";
import { api, rest, type LiveSession, type SessionItem } from "./api";
import { when } from "./shared";
import { ProfileContext } from "./profiles";
import { Banner, Button, ConfirmDialog, type Tone } from "./ui";

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
