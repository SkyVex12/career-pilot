// Coding mode's bar on the overlay (ARCHITECTURE.md, "Coding mode"): Add shot puts a shot of
// the window in front into the tray (scroll the problem or switch files, and add another, up to
// 6); Solve sends the tray as one problem, or with an empty tray takes one shot and sends it.
// Both have hotkeys, so the problem's window keeps the focus. The answer mode (Interview: say
// it, then type it; Assessment: paste it) and the language (Auto lets the screen decide) are
// kept across calls. The shots are taken and sent by the app (src-tauri/src/shots.rs).
import { useEffect, useState } from "react";
import { api, inTauri, onShotError, onShots, type CodingMode, type ShotView } from "./api";
import { prettyKey } from "./hotkeys";
import { Select, cx } from "./ui";

export const LANGUAGES = ["", "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Kotlin", "Swift", "Ruby", "Rust", "SQL"];
const MAX_SHOTS = 6;

export function shotTitle(s: ShotView): string {
  const kb = Math.max(1, Math.round(s.bytes / 1024));
  const from = s.source === "window" ? s.title || "window" : "screen";
  return `${from} · ${s.width}×${s.height} · ${kb} KB · ${s.capture_ms + s.encode_ms} ms`;
}

export function CodingBar({ running, collapsible = false }: { running: boolean; collapsible?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [shots, setShots] = useState<ShotView[]>([]);
  const [mode, setMode] = useState<CodingMode>("interview");
  const [language, setLanguage] = useState("");
  const [keys, setKeys] = useState({ add: "", solve: "" });
  const [busy, setBusy] = useState<"add" | "solve" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!inTauri) return;
    let live = true;
    // Read again when a session starts: starting an online test switches the mode to Test.
    void api.getSettings().then((v) => {
      if (!v?.settings) return;
      if (!live) return;
      setMode(v.settings.coding_mode === "assessment" ? "assessment" : "interview");
      setLanguage(v.settings.coding_language ?? "");
      setKeys({ add: v.settings.add_shot_hotkey, solve: v.settings.solve_hotkey });
    });
    void api.codingShots().then((s) => live && setShots(Array.isArray(s) ? s : []), () => undefined);
    const offs = [
      onShots((s) => {
        setShots(s);
        setError(null);
      }),
      onShotError(setError),
    ];
    return () => {
      live = false;
      offs.forEach((p) => void p.then((off) => off()));
    };
  }, [running]);

  const prefs = (m: CodingMode, l: string) => {
    setMode(m);
    setLanguage(l);
    if (inTauri) void api.setCodingPrefs(m, l).catch(() => undefined);
  };
  const run = (what: "add" | "solve") => {
    setBusy(what);
    setError(null);
    (what === "add" ? api.codingAddShot() : api.codingSolve()).then(
      () => setBusy(null),
      (e) => {
        setBusy(null);
        setError(String(e));
      },
    );
  };
  const full = shots.length >= MAX_SHOTS;
  const n = shots.length;
  useEffect(() => { if (n > 0 || error) setExpanded(true); }, [n, error]);
  const toolsOpen = expanded;

  return (
    <section className="ov-coding" aria-label="Coding">
      {collapsible && <button className="ov-coding-toggle" type="button" aria-expanded={toolsOpen} onClick={() => setExpanded(!expanded)}>Screen tools <span aria-hidden="true">{toolsOpen ? "−" : "+"}</span></button>}
      <div hidden={collapsible && !toolsOpen}>
      <div className="ov-coding-row">
        <button type="button" className="ov-ask-btn" disabled={busy !== null || full} onClick={() => run("add")} title={full ? "6 shots at most" : `A shot of the window in front${keys.add ? ` (${prettyKey(keys.add)})` : ""}`}>
          {busy === "add" ? "Adding…" : "+ Shot"}
        </button>
        <button
          type="button"
          className="ov-ask-btn ov-coding-solve"
          disabled={busy !== null || !running}
          onClick={() => run("solve")}
          title={!running ? "Start a session first" : `${n ? `Solve these ${n} shots` : "Take one shot and solve it"}${keys.solve ? ` (${prettyKey(keys.solve)})` : ""}`}
        >
          {busy === "solve" ? "Sending…" : n ? `Solve ${n}` : "Solve"}
        </button>
        <div className="ov-seg" role="radiogroup" aria-label="Answer mode">
          {(["interview", "assessment"] as const).map((m) => (
            <button key={m} type="button" role="radio" aria-checked={mode === m} className={cx("ov-seg-btn", mode === m && "is-on")} onClick={() => prefs(m, language)} title={m === "interview" ? "A call is on: what to say first, then code to type while talking" : "An online test: the answer or code first, ready to paste"}>
              {m === "interview" ? "Interview" : "Test"}
            </button>
          ))}
        </div>
        <Select className="ov-coding-lang" aria-label="Code language" value={language} onChange={(e) => prefs(mode, e.target.value)} title="The code's language; Auto takes it from the screen">
          {LANGUAGES.map((l) => (
            <option key={l} value={l}>
              {l || "Auto"}
            </option>
          ))}
        </Select>
      </div>
      {n > 0 && (
        <ol className="ov-shots" aria-label="Shots to solve">
          {shots.map((s, i) => (
            <li key={s.id} className="ov-shot" title={shotTitle(s)}>
              <img src={s.url} alt={`Shot ${i + 1}: ${s.title ?? "screen"}`} />
              <span className="ov-shot-n" aria-hidden="true">
                {i + 1}
              </span>
              <button type="button" className="ov-shot-x" aria-label={`Remove shot ${i + 1}`} onClick={() => void api.codingRemoveShot(s.id)}>
                ×
              </button>
            </li>
          ))}
        </ol>
      )}
      {error && (
        <p className="ov-ask-error" role="alert">
          {error}
        </p>
      )}
      </div>
    </section>
  );
}
