// The main window: the session (start screen, then the post-call screen), documents, jobs
// and interviews, history, settings, and the debug panel (hidden unless turned on in the
// settings, or with Ctrl+Shift+D).
import { useEffect, useState } from "react";
import type { Mode } from "@copilot/protocol";
import { api, inTauri, type DeviceInfo, type LiveStatus, type Settings } from "./api";
import { parseTarget, upcoming } from "./catalog";
import { Documents, when } from "./Documents";
import { History } from "./History";
import { Jobs, roundLabel, useCatalog } from "./Jobs";
import { SessionDetail } from "./SessionDetail";
import { badge, useSession, type SessionView } from "./session";

type Tab = "start" | "documents" | "jobs" | "history" | "settings" | "debug";

const TABS: [Tab, string][] = [
  ["start", "Session"],
  ["documents", "Documents"],
  ["jobs", "Jobs"],
  ["history", "History"],
  ["settings", "Settings"],
];

export default function App() {
  const [session] = useSession();
  const [tab, setTab] = useState<Tab>("start");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [tokenSet, setTokenSet] = useState(false);
  const [showDebug, setShowDebug] = useState(false);

  const reload = () => {
    api.getSettings().then(([s, t]) => {
      setSettings(s);
      setTokenSet(t);
      setShowDebug(s.show_debug);
    });
  };
  useEffect(() => {
    if (inTauri) reload();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "d") {
        setShowDebug((v) => !v);
        setTab((t) => (t === "debug" ? "start" : "debug"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!inTauri) {
    return (
      <main className="page">
        <h1>Copilot</h1>
        <p>This UI needs the Rust core: run <code>pnpm --filter @copilot/desktop tauri dev</code>.</p>
      </main>
    );
  }

  const needsToken = !tokenSet && (tab === "documents" || tab === "jobs" || tab === "history");
  return (
    <main className="page">
      <nav className="tabs">
        {TABS.map(([t, label]) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
        {showDebug && (
          <button className={tab === "debug" ? "on" : ""} onClick={() => setTab("debug")}>
            Debug
          </button>
        )}
        <span className="spacer" />
        <StatusBadge session={session} />
      </nav>
      {tab === "start" && <StartScreen session={session} tokenSet={tokenSet} onSettings={() => setTab("settings")} />}
      {needsToken && <SetTokenFirst onSettings={() => setTab("settings")} />}
      {!needsToken && tab === "documents" && <Documents />}
      {!needsToken && tab === "jobs" && <Jobs />}
      {!needsToken && tab === "history" && <History />}
      {tab === "settings" && settings && (
        <SettingsScreen settings={settings} tokenSet={tokenSet} running={session.running} onSaved={reload} />
      )}
      {tab === "debug" && <DebugPanel session={session} />}
    </main>
  );
}

function StatusBadge({ session }: { session: SessionView }) {
  const b = badge(session);
  return (
    <span className={`badge badge-${b.state}`}>
      <span className="dot" />
      {b.text}
    </span>
  );
}

function SetTokenFirst({ onSettings }: { onSettings: () => void }) {
  return (
    <p className="callout">
      Set the dev token first. <button className="link" onClick={onSettings}>Open settings</button>
    </p>
  );
}

const PLATFORMS = [
  ["zoom", "Zoom"],
  ["meet", "Google Meet"],
  ["teams", "Microsoft Teams"],
  ["other", "Other"],
];

function StartScreen({ session, tokenSet, onSettings }: { session: SessionView; tokenSet: boolean; onSettings: () => void }) {
  const [mode, setMode] = useState<Mode>("interview");
  const catalog = useCatalog(tokenSet);
  const [target, setTarget] = useState("");
  const [title, setTitle] = useState("");
  const [platform, setPlatform] = useState("zoom");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [postCall, setPostCall] = useState<string | null>(null);

  const interviews = upcoming(catalog.interviews ?? []);
  useEffect(() => {
    // Default to the next interview, else the newest job.
    if (target) return;
    if (interviews.length) setTarget(`interview:${interviews[0].id}`);
    else if (catalog.jobs?.length) setTarget(`job:${catalog.jobs[0].id}`);
  }, [catalog.jobs, catalog.interviews]); // eslint-disable-line react-hooks/exhaustive-deps

  // The post-call screen for the session that just ended.
  useEffect(() => {
    if (session.running) setPostCall(null);
    else if (session.stopped === "ended" && session.sessionId) setPostCall(session.sessionId);
  }, [session.stopped, session.sessionId, session.running]);

  const start = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const picked = mode === "interview" ? parseTarget(target, catalog.interviews ?? []) : { job_id: null, interview_id: null };
      await api.startSession({
        mode,
        ...picked,
        meeting_title: title.trim() || null,
        platform,
        store_transcript: consent,
      });
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  const endOther = async () => {
    setMessage(null);
    try {
      setMessage(`${await api.endOtherSession()}. You can start now.`);
    } catch (e) {
      setMessage(String(e));
    }
  };

  const running = session.running;
  return (
    <section className="panel">
      {!tokenSet && <SetTokenFirst onSettings={onSettings} />}
      <div className="grid">
        <label>Mode</label>
        <div className="segmented">
          {(["interview", "standup", "meeting"] as Mode[]).map((m) => (
            <button
              key={m}
              className={mode === m ? "on" : ""}
              onClick={() => {
                setMode(m);
                if (running) api.setMode(m).catch(() => undefined);
              }}
            >
              {m}
            </button>
          ))}
        </div>

        <label>Call for</label>
        <div>
          <select value={target} disabled={running || mode !== "interview"} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Nothing specific (the mode's pack, if any)</option>
            {interviews.length > 0 && (
              <optgroup label="Interviews">
                {interviews.map((i) => (
                  <option key={i.id} value={`interview:${i.id}`}>
                    {[i.company, i.title, roundLabel(i.round), when(i.starts_at)].filter(Boolean).join(" · ")}
                    {packNote(i.pack)}
                  </option>
                ))}
              </optgroup>
            )}
            {(catalog.jobs?.length ?? 0) > 0 && (
              <optgroup label="Jobs">
                {catalog.jobs?.map((j) => (
                  <option key={j.id} value={`job:${j.id}`}>
                    {j.company} · {j.title}
                    {packNote(j.pack)}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button className="link" onClick={catalog.reload} disabled={!tokenSet}>
            Refresh
          </button>
          {catalog.error && <p className="note warn">{catalog.error}</p>}
          {catalog.jobs?.length === 0 && <p className="note">No jobs yet: upload a job description under Documents.</p>}
        </div>

        <label>Meeting title</label>
        <input value={title} disabled={running} onChange={(e) => setTitle(e.target.value)} placeholder="Optional" />

        <label>Call app</label>
        <select value={platform} disabled={running} onChange={(e) => setPlatform(e.target.value)}>
          {PLATFORMS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>

        <label>Transcript</label>
        <div>
          <label className="check">
            <input type="checkbox" checked={consent} disabled={running} onChange={(e) => setConsent(e.target.checked)} />
            Store the transcript after the call (everyone on the call has agreed)
          </label>
          <p className="note">
            {consent
              ? "Your own audio is recorded on this computer, encrypted, uploaded after the call for transcription, then deleted."
              : "Nothing is recorded or stored; the summary covers only the call's length and the questions' types."}
          </p>
        </div>
      </div>

      <div className="actions">
        {!running ? (
          <button className="primary" disabled={busy || !tokenSet} onClick={start}>
            Start session
          </button>
        ) : (
          <button className="danger" onClick={() => api.endSession().catch((e) => setMessage(String(e)))}>
            End session
          </button>
        )}
        <button onClick={() => api.showOverlay(true)}>Show overlay</button>
      </div>

      {session.stopped === "session_active" && (
        <p className="callout">
          Another session is active for this user. <button className="link" onClick={endOther}>End the other session</button>
        </p>
      )}
      {session.stopped === "no_minutes" && <p className="callout">No minutes left on the plan.</p>}
      {session.stopped === "unauthorized" && <p className="callout">The gateway rejected the dev token. Check it in Settings.</p>}
      {message && <p className="note warn">{message}</p>}
      {postCall && !running && (
        <div className="postcall">
          <SessionDetail sessionId={postCall} onClose={() => setPostCall(null)} />
        </div>
      )}
    </section>
  );
}

function packNote(pack: { available: boolean } | null | undefined): string {
  if (!pack) return " (pack building)";
  return pack.available ? "" : " (pack expired)";
}

function SettingsScreen({
  settings,
  tokenSet,
  running,
  onSaved,
}: {
  settings: Settings;
  tokenSet: boolean;
  running: boolean;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(settings);
  const [token, setToken] = useState("");
  const [devices, setDevices] = useState<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    api.listDevices().then(setDevices);
  }, []);

  const save = async () => {
    setMessage(null);
    try {
      await api.saveSettings(draft, token ? token : null);
      setToken("");
      setMessage(running ? "Saved. Device and echo settings apply to the next session." : "Saved.");
      onSaved();
    } catch (e) {
      setMessage(String(e));
    }
  };
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setDraft({ ...draft, [k]: v });
  const deviceSelect = (list: DeviceInfo[] | undefined, value: string | null, key: "mic_device" | "output_device") => (
    <select value={value ?? ""} onChange={(e) => set(key, e.target.value || null)}>
      <option value="">System default</option>
      {list?.map((d) => (
        <option key={d.id} value={d.id}>
          {d.name}
          {d.is_default ? " (default)" : ""}
        </option>
      ))}
    </select>
  );

  return (
    <section className="panel">
      <div className="grid">
        <label>Gateway URL</label>
        <input value={draft.gateway_url} onChange={(e) => set("gateway_url", e.target.value)} />

        <label>API URL</label>
        <div>
          <input value={draft.api_url} onChange={(e) => set("api_url", e.target.value)} />
          <p className="note">The Copilot API's /v1 root, for example http://127.0.0.1:8080/v1. The same token signs in to both.</p>
        </div>

        <label>Dev token</label>
        <div>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={tokenSet ? "Saved (type to replace)" : "COPILOT_DEV_TOKEN from the .env"}
          />
          <p className="note">Kept in the Windows Credential Manager, not in the settings file. Cognito sign-in replaces it in build step 5.</p>
        </div>

        <label>Microphone</label>
        {deviceSelect(devices?.inputs, draft.mic_device, "mic_device")}

        <label>Call audio from</label>
        <div>
          {deviceSelect(devices?.outputs, draft.output_device, "output_device")}
          <p className="note">The speakers or headset the call plays on; captured in loopback.</p>
        </div>

        <label>Echo cancellation</label>
        <label className="check">
          <input type="checkbox" checked={draft.echo_cancellation} onChange={(e) => set("echo_cancellation", e.target.checked)} />
          Remove the call's sound from the microphone (needed without headphones)
        </label>

        <label>Hotkey</label>
        <div>
          <input value={draft.hotkey} onChange={(e) => set("hotkey", e.target.value)} />
          <p className="note">Answers the last thing said when detection missed it. Example: CommandOrControl+Shift+Space.</p>
        </div>

        <label>Debug panel</label>
        <label className="check">
          <input type="checkbox" checked={draft.show_debug} onChange={(e) => set("show_debug", e.target.checked)} />
          Show (also Ctrl+Shift+D)
        </label>
      </div>
      <div className="actions">
        <button className="primary" onClick={save}>
          Save
        </button>
        {message && <span className="note">{message}</span>}
      </div>
      <Meters />
    </section>
  );
}

function usePoll(ms: number): LiveStatus | null {
  const [live, setLive] = useState<LiveStatus | null>(null);
  useEffect(() => {
    const t = setInterval(() => api.liveStatus().then(setLive), ms);
    return () => clearInterval(t);
  }, [ms]);
  return live;
}

function Meter({ label, rms, speech }: { label: string; rms: number; speech: number }) {
  const db = rms > 0 ? Math.max(0, Math.min(1, (20 * Math.log10(rms) + 60) / 60)) : 0;
  return (
    <div className="meter">
      <span>{label}</span>
      <div className="bar">
        <div style={{ width: `${db * 100}%` }} />
      </div>
      <span className="muted">speech {(speech * 100).toFixed(0)}%</span>
    </div>
  );
}

function Meters() {
  const live = usePoll(200);
  if (!live?.levels) return <p className="note">Levels show while a session runs.</p>;
  const l = live.levels;
  return (
    <div className="meters">
      <Meter label="Call (them)" rms={l.them_rms} speech={l.them_speech} />
      <Meter label="Mic (you, after AEC)" rms={l.me_rms} speech={l.me_speech} />
    </div>
  );
}

function ms(v: number | null | undefined): string {
  return v == null ? "–" : `${v}`;
}

function DebugPanel({ session }: { session: SessionView }) {
  const live = usePoll(500);
  const latency = Object.values(session.latency);
  return (
    <section className="panel debug">
      <h3>Latency per question (ms)</h3>
      <table>
        <thead>
          <tr>
            <th>Question</th>
            <th>From</th>
            <th title="Client: speech end to question.detected">detected</th>
            <th title="Client: speech end to the first fast answer word">first word</th>
            <th title="Client: speech end to the first deep answer word">deep word</th>
            <th title="Gateway latency_ms of the fast pass">gw turn_end / detector / first_token / first_delta / total</th>
          </tr>
        </thead>
        <tbody>
          {latency.map((l) => {
            const g = l.fast_gateway;
            return (
              <tr key={l.question_id}>
                <td className="clip" title={l.text}>
                  {l.text}
                </td>
                <td>{l.anchor.replace("_", " ")}</td>
                <td>{ms(l.detected_ms)}</td>
                <td className={l.fast_first_word_ms != null && l.fast_first_word_ms > 1500 ? "warn" : ""}>{ms(l.fast_first_word_ms)}</td>
                <td>{ms(l.deep_first_word_ms)}</td>
                <td>{g ? [g.turn_end_ms, g.detector_ms, g.first_token_ms, g.first_delta_ms, g.total_ms].map(ms).join(" / ") : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h3>Detected questions</h3>
      <ul className="plain">
        {session.questions.map((q) => (
          <li key={q.id} className={q.cancelled ? "muted strike" : ""}>
            <code>{q.type}</code> {q.confidence.toFixed(2)} {q.speculative ? "(speculative)" : ""} {q.cancelled ? "(cancelled)" : ""} {q.text}
          </li>
        ))}
      </ul>

      <h3>
        Transcript <span className="muted">them {session.speaking.them ? "●" : "○"} · you {session.speaking.me ? "●" : "○"}</span>
      </h3>
      <ul className="plain transcript">
        {session.transcript.slice(-40).map((t) => (
          <li key={t.key} className={t.final ? "" : "muted"}>
            <code>{t.channel}</code> {t.text}
          </li>
        ))}
      </ul>

      <h3>Connection</h3>
      {live?.stats && (
        <p className="note">
          session {live.session_id ?? "–"} · connects {live.stats.connects} · resumes {live.stats.resumes} · frames sent{" "}
          {live.stats.frames_sent}, resent {live.stats.frames_resent}, dropped {live.stats.frames_dropped} · pings {live.stats.pings_sent} ·
          closes {live.stats.close_codes.map((c) => c ?? "drop").join(", ") || "none"}
          {live.levels && ` · capture underruns them ${live.levels.them_underruns}, me ${live.levels.me_underruns}`}
        </p>
      )}
      <pre className="log">{session.log.join("\n")}</pre>
    </section>
  );
}
