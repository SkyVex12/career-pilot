// The always-on-top overlay: talking points first, the deep answer below with an expand
// control, thumbs, the status badge, and a box to type a question.
import { useEffect, useMemo, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { api } from "./api";
import { AnswerText } from "./AnswerText";
import { AUTO_DEEP, badge, useSession, type QuestionView } from "./session";

export default function Overlay() {
  const [s, dispatch] = useSession();
  const visible = useMemo(() => s.questions.filter((q) => !q.cancelled), [s.questions]);
  // Follow the newest question unless the user went back to an older one.
  const [pinned, setPinned] = useState<string | null>(null);
  const index = pinned ? visible.findIndex((q) => q.id === pinned) : visible.length - 1;
  const q = index >= 0 ? visible[index] : undefined;
  const b = badge(s);

  return (
    <div className="overlay">
      <header className="overlay-head" data-tauri-drag-region>
        <span className={`badge badge-${b.state}`} title={b.text} data-tauri-drag-region>
          <span className="dot" />
          {b.text}
        </span>
        <span className="speaking" data-tauri-drag-region>
          {s.speaking.them && <span className="pill">them</span>}
          {s.speaking.me && <span className="pill pill-me">you</span>}
        </span>
        <span className="spacer" data-tauri-drag-region />
        {visible.length > 1 && (
          <span className="nav">
            <button disabled={index <= 0} onClick={() => setPinned(visible[index - 1].id)} title="Older question">
              ‹
            </button>
            <span className="muted">
              {index + 1}/{visible.length}
            </span>
            <button
              disabled={index >= visible.length - 1}
              onClick={() => setPinned(index + 1 >= visible.length - 1 ? null : visible[index + 1].id)}
              title="Newer question"
            >
              ›
            </button>
          </span>
        )}
        <button className="icon" title="Hide overlay" onClick={() => getCurrentWindow().hide()}>
          ×
        </button>
      </header>

      <main className="overlay-body">
        {q ? (
          <QuestionCard key={q.id} q={q} onExpand={() => dispatch({ type: "deep_requested", id: q.id })} />
        ) : (
          <p className="muted empty">
            {s.running ? "Waiting for a question. Press the hotkey to answer the last thing said." : "Start a session in the main window."}
          </p>
        )}
      </main>

      <AskBox disabled={!s.running} />
    </div>
  );
}

function QuestionCard({ q, onExpand }: { q: QuestionView; onExpand: () => void }) {
  const [open, setOpen] = useState(false);
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const deepStarted = q.deep.text.length > 0 || q.deep.done || q.deepRequested;
  useEffect(() => setOpen(false), [q.id]);

  const expand = () => {
    if (!deepStarted) {
      onExpand();
      api.expandAnswer(q.id).catch(() => undefined);
    }
    setOpen((o) => !o || !deepStarted);
  };
  const rate = (up: boolean) => {
    setRating(up ? "up" : "down");
    api.sendFeedback(q.id, up).catch(() => undefined);
  };

  return (
    <article className="card">
      <h2 className="question">
        {q.text}
        {q.speculative && <span className="tag">listening…</span>}
        <span className="tag">{q.type.replace("_", " ")}</span>
      </h2>

      <section className="points">
        {q.fast.text ? <AnswerText text={q.fast.text} /> : !q.fast.done && <p className="muted">Thinking…</p>}
        {q.fast.source === "prepared" && <p className="note">Prepared answer</p>}
        {q.fast.error && q.fast.error !== "cancelled" && <p className="note warn">Talking points failed ({q.fast.error})</p>}
        {q.fast.error === "cancelled" && <p className="note">Stopped for a newer question</p>}
      </section>

      <section className="deep">
        <button className="link" onClick={expand}>
          {!deepStarted ? "Get the detailed answer ▾" : open ? "Hide the detailed answer ▴" : "Show the detailed answer ▾"}
        </button>
        {deepStarted && (
          <div className={open ? "deep-text" : "deep-text collapsed"} onClick={() => setOpen(true)}>
            {q.deep.text ? <AnswerText text={q.deep.text} /> : !q.deep.done && <p className="muted">Writing…</p>}
            {q.deep.error && q.deep.error !== "cancelled" && <p className="note warn">Detailed answer failed ({q.deep.error})</p>}
          </div>
        )}
        {!AUTO_DEEP.includes(q.type) && !deepStarted && <p className="note">Only on request for this question type.</p>}
      </section>

      <footer className="thumbs">
        <button className={rating === "up" ? "on" : ""} onClick={() => rate(true)} title="Helpful">
          👍
        </button>
        <button className={rating === "down" ? "on" : ""} onClick={() => rate(false)} title="Not helpful">
          👎
        </button>
        {q.fast.refs.length > 0 && (
          <span className="refs muted" title={q.fast.refs.map((r) => r.label).join("\n")}>
            from {q.fast.refs.map((r) => r.label).join(", ")}
          </span>
        )}
      </footer>
    </article>
  );
}

function AskBox({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = (value: string | null) => {
    setError(null);
    api.forceQuestion(value).then(
      () => setText(""),
      (e) => setError(String(e)),
    );
  };
  return (
    <form
      className="ask"
      onSubmit={(e) => {
        e.preventDefault();
        send(text.trim() || null);
      }}
    >
      <input
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        placeholder={disabled ? "No session" : "Type a question, or Enter to answer what was just said"}
      />
      <button type="submit" disabled={disabled}>
        Ask
      </button>
      {error && <span className="note warn">{error}</span>}
    </form>
  );
}
