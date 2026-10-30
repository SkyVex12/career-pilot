// The debug panel (Ctrl+Shift+D, or Settings, Advanced): audio levels, latency per question,
// detected questions, the transcript and the connection log. A developer tool.
import { useEffect, useState } from "react";
import { api, type LiveStatus } from "./api";
import type { SessionView } from "./session";
import { Card, PageHeader } from "./ui";

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
    <div className="debug-meter">
      <span>{label}</span>
      <span className="level-bar">
        <span className="level-fill" style={{ width: `${db * 100}%` }} />
      </span>
      <span className="ui-muted ui-num">speech {(speech * 100).toFixed(0)}%</span>
    </div>
  );
}

function ms(v: number | null | undefined): string {
  return v == null ? "–" : `${v}`;
}

export function DebugPanel({ session }: { session: SessionView }) {
  const live = usePoll(500);
  const latency = Object.values(session.latency);
  const l = live?.levels;
  return (
    <>
      <PageHeader title="Debug" />
      <Card title="Audio levels">
        {l ? (
          <div className="ui-stack-tight">
            <Meter label="Call (them)" rms={l.them_rms} speech={l.them_speech} />
            <Meter label="Mic (you, after AEC)" rms={l.me_rms} speech={l.me_speech} />
          </div>
        ) : (
          <p className="ui-field-hint">Levels show while a call runs.</p>
        )}
      </Card>
      <Card title="Latency per question (ms)">
        <div className="debug-scroll">
          <table className="debug-table">
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
              {latency.map((q) => {
                const g = q.fast_gateway;
                return (
                  <tr key={q.question_id}>
                    <td className="ui-truncate debug-q" title={q.text}>
                      {q.text}
                    </td>
                    <td>{q.anchor.replace("_", " ")}</td>
                    <td className="ui-num">{ms(q.detected_ms)}</td>
                    <td className={q.fast_first_word_ms != null && q.fast_first_word_ms > 1500 ? "ui-num debug-slow" : "ui-num"}>{ms(q.fast_first_word_ms)}</td>
                    <td className="ui-num">{ms(q.deep_first_word_ms)}</td>
                    <td className="ui-num">{g ? [g.turn_end_ms, g.detector_ms, g.first_token_ms, g.first_delta_ms, g.total_ms].map(ms).join(" / ") : "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Detected questions">
        <ul className="debug-lines">
          {session.questions.map((q) => (
            <li key={q.id} className={q.cancelled ? "ui-muted debug-strike" : ""}>
              <code>{q.type}</code> {q.confidence.toFixed(2)} {q.speculative ? "(speculative)" : ""} {q.cancelled ? "(cancelled)" : ""} {q.text}
            </li>
          ))}
        </ul>
      </Card>
      <Card title={`Transcript · them ${session.speaking.them ? "●" : "○"} · you ${session.speaking.me ? "●" : "○"}`}>
        <ul className="debug-lines">
          {session.transcript.slice(-40).map((t) => (
            <li key={t.key} className={t.final ? "" : "ui-muted"}>
              <code>{t.channel}</code> {t.text}
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Connection">
        {live?.stats && (
          <p className="ui-field-hint">
            session {live.session_id ?? "–"} · connects {live.stats.connects} · resumes {live.stats.resumes} · frames sent {live.stats.frames_sent}, resent{" "}
            {live.stats.frames_resent}, dropped {live.stats.frames_dropped} · pings {live.stats.pings_sent} · closes{" "}
            {live.stats.close_codes.map((c) => c ?? "drop").join(", ") || "none"}
            {live.levels && ` · capture underruns them ${live.levels.them_underruns}, me ${live.levels.me_underruns}`}
          </p>
        )}
        <pre className="debug-log">{session.log.join("\n")}</pre>
      </Card>
    </>
  );
}
