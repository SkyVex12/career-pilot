// The main window: start screen, settings, and the debug panel (hidden unless turned on in
// the settings, or with Ctrl+Shift+D).
import { useCallback, useEffect, useState } from "react";
import type { Mode } from "@copilot/protocol";
import { api, inTauri, type DeviceInfo, type JobList, type LiveStatus, type Settings } from "./api";
import { badge, useSession, type SessionView } from "./session";

type Tab = "start" | "settings" | "debug";

export default function App() {
  const [session] = useSession();
  const [tab, setTab] = useState<Tab>("start");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [tokenSet, setTokenSet] = useState(false);
  const [showDebug, setShowDebug] = useState(false);

  const reload = useCallback(() => {
    api.getSettings().then(([s, t]) => {
      setSettings(s);
      setTokenSet(t);
      setShowDebug(s.show_debug);
    });
  }, []);
  useEffect(() => {
    if (inTauri) reload();
  }, [reload]);
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

  return (
    <main className="page">
      <nav className="tabs">
        <button className={tab === "start" ? "on" : ""} onClick={() => setTab("start")}>
          Session
        </button>
        <button className={tab === "settings" ? "on" : ""} onClick={() => setTab("settings")}>
          Settings
        </button>
        {showDebug && (
          <button className={tab === "debug" ? "on" : ""} onClick={() => setTab("debug")}>
            Debug
          </button>
        )}
        <span className="spacer" />
        <StatusBadge session={session} />
      </nav>
      {tab === "start" && <StartScreen session={session} tokenSet={tokenSet} onSettings={() => setTab("settings")} />}
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

const PLATFORMS = [
  ["zoom", "Zoom"],
  ["meet", "Google Meet"],
  ["teams", "Microsoft Teams"],
  ["other", "Other"],
];

function StartScreen({ session, tokenSet, onSettings }: { session: SessionView; tokenSet: boolean; onSettings: () => void }) {
  const [mode, setMode] = useState<Mode>("interview");
  const [jobs, setJobs] = useState<JobList | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string>("");
  const [title, setTitle] = useState("");
  const [platform, setPlatform] = useState("zoom");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadJobs = useCallback(() => {
    setJobsError(null);
    api.listJobs().then(
      (j) => {
        setJobs(j);
        setJobId((cur) => cur || j.jobs.find((x) => x.pack_available)?.job_id || "");
      },
      (e) => setJobsError(String(e)),
    );
  }, []);
  useEffect(() => {
    if (tokenSet) loadJobs();
  }, [tokenSet, loadJobs]);

  const start = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api.startSession({
        mode,
        job_id: mode === "interview" && jobId ? jobId : null,
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
      {!tokenSet && (
        <p className="callout">
          Set the gateway dev token first. <button className="link" onClick={onSettings}>Open settings</button>
        </p>
      )}
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

        <label>Job</label>
        <div>
          <select value={jobId} disabled={running || mode !== "interview"} onChange={(e) => setJobId(e.target.value)}>
            <option value="">None (the mode's pack, if any)</option>
            {jobs?.jobs.map((j) => (
              <option key={j.job_id} value={j.job_id} disabled={!j.pack_available}>
                {[j.company, j.role_title, j.round].filter(Boolean).join(" · ") || j.job_id}
                {j.pack_available ? "" : " (pack expired; rerun dev_profile.py)"}
              </option>
            ))}
          </select>
          <button className="link" onClick={loadJobs} disabled={!tokenSet}>
            Refresh
          </button>
          {jobsError && <p className="note warn">{jobsError}</p>}
          {jobs && jobs.jobs.length === 0 && <p className="note">No jobs yet: run scripts/dev_profile.py.</p>}
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
        <label className="check">
          <input type="checkbox" checked={consent} disabled={running} onChange={(e) => setConsent(e.target.checked)} />
          Store the transcript after the call (everyone on the call has agreed)
        </label>
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
      {session.stopped === "ended" && !running && <p className="note">Session ended.</p>}
      {message && <p className="note warn">{message}</p>}
    </section>
  );
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

        <label>Dev token</label>
        <div>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={tokenSet ? "Saved (type to replace)" : "COPILOT_DEV_TOKEN from the gateway's .env"}
          />
          <p className="note">Kept in the Windows Credential Manager, not in the settings file.</p>
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
