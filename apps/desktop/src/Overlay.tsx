// The always-on-top overlay: the whole conversation as one scrolling view that never drops
// earlier messages. The other side on the left (live text that settles into the final), the
// suggested answers on the right (talking points first, the detailed answer streaming in
// below), and the user's own words on the right as plain quoted lines, marked "You said", so
// they never read as an answer. It follows the newest message while the reader is at the
// bottom; scrolled up, it stays put and offers "Jump to latest". Hotkey, typed question,
// thumbs and the status badge as before; a compact mode for a small window.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { api, inTauri, onOverlayPrefs, type OverlayPrefs } from "./api";
import { AnswerText } from "./AnswerText";
import { copier, Follower } from "./follow";
import { badge, conversation, deepStartsAlone, useSession, type ConversationItem, type QuestionView, type SessionView } from "./session";

const COMPACT_KEY = "overlay.compact";

function loadCompact(): boolean {
  try {
    return window.localStorage.getItem(COMPACT_KEY) === "1";
  } catch {
    return false;
  }
}

function saveCompact(on: boolean) {
  try {
    window.localStorage.setItem(COMPACT_KEY, on ? "1" : "0");
  } catch {
    // Per-window convenience only.
  }
}

/** The overlay's comfort prefs, kept by the Rust core per monitor: opacity and view it renders,
 * and whether clicks are passing through it. The core pushes changes (a hotkey, or a move to
 * another monitor); the overlay asks once on mount. `compact` keeps a localStorage copy so the
 * first paint matches, before the core answers. */
function useOverlayPrefs() {
  const [opacity, setOpacity] = useState(1);
  const [compact, setCompactState] = useState(loadCompact);
  const [clickThrough, setClickThrough] = useState(false);
  const [offHotkey, setOffHotkey] = useState("");

  useEffect(() => {
    if (!inTauri) return;
    let live = false;
    const apply = (p: OverlayPrefs) => {
      setOpacity(p.opacity);
      setCompactState(p.compact);
      saveCompact(p.compact);
      setClickThrough(p.click_through);
      setOffHotkey(p.click_through_off);
    };
    const off = onOverlayPrefs((p) => {
      live = true;
      apply(p);
    });
    api
      .overlayPrefs()
      .then((p) => {
        if (!live) apply(p);
      })
      .catch(() => undefined);
    return () => {
      off.then((f) => f());
    };
  }, []);

  const setCompact = (on: boolean) => {
    setCompactState(on);
    saveCompact(on);
    if (inTauri) api.setOverlayView(on).catch(() => undefined);
  };
  return { opacity, compact, clickThrough, offHotkey, setCompact };
}

/// Changes whenever anything shown grows, so the view can follow it.
function contentSize(items: ConversationItem[]): string {
  let chars = 0;
  for (const it of items) {
    if (it.kind === "question") chars += it.question.fast.text.length + it.question.deep.text.length + (it.question.deep.done ? 1 : 0);
    else chars += it.text.length;
  }
  return `${items.length}:${chars}`;
}

export default function Overlay() {
  const [s, dispatch] = useSession();
  const items = useMemo(() => conversation(s), [s]);
  const { opacity, compact, clickThrough, offHotkey, setCompact } = useOverlayPrefs();
  const shown = useMemo(() => (compact ? items.filter((i) => i.kind === "question") : items), [items, compact]);
  const size = contentSize(shown);
  const body = useRef<HTMLDivElement>(null);
  const follower = useRef(new Follower());
  const [jump, setJump] = useState(false);
  const b = badge(s);

  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    follower.current.onContent(el);
    setJump(follower.current.showJump);
  }, [size, compact]);

  const onScroll = () => {
    const el = body.current;
    if (!el) return;
    follower.current.onScroll(el);
    setJump(follower.current.showJump);
  };
  const toLatest = () => {
    const el = body.current;
    if (!el) return;
    follower.current.jump(el);
    setJump(false);
  };
  const toggleCompact = () => setCompact(!compact);

  return (
    <div className={compact ? "overlay compact" : "overlay"} style={{ opacity }}>
      {clickThrough && (
        <div className="clickthrough" role="status">
          Click-through on{offHotkey ? ` — ${offHotkey} to turn off` : " — use your turn-off hotkey"}
        </div>
      )}
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
        <button className="icon" title={compact ? "Show the whole conversation" : "Compact: answers only"} onClick={toggleCompact}>
          {compact ? "☰" : "▭"}
        </button>
        <button
          className="icon"
          title="Click-through: let clicks pass to the window underneath (a hotkey turns it back off)"
          onClick={() => inTauri && api.setClickThrough(true).catch(() => undefined)}
        >
          ⇲
        </button>
        <button className="icon" title="Hide overlay" onClick={() => inTauri && getCurrentWindow().hide()}>
          ×
        </button>
      </header>

      <main className="overlay-body" ref={body} onScroll={onScroll}>
        {shown.length === 0 ? (
          <p className="muted empty">
            {s.running ? "Listening. Press the hotkey to answer the last thing said." : "Start a session in the main window."}
          </p>
        ) : (
          <ol className="conversation">
            {shown.map((it) => (
              <Item key={it.key} item={it} s={s} compact={compact} onExpand={(id) => dispatch({ type: "deep_requested", id })} />
            ))}
          </ol>
        )}
      </main>

      {jump && (
        <button className="jump" onClick={toLatest}>
          Jump to latest ↓
        </button>
      )}

      <AskBox disabled={!s.running} />
      <span className="grip" title="Resize" onMouseDown={() => inTauri && getCurrentWindow().startResizeDragging("SouthEast")} />
    </div>
  );
}

function Item({ item, s, compact, onExpand }: { item: ConversationItem; s: SessionView; compact: boolean; onExpand: (id: string) => void }) {
  switch (item.kind) {
    case "them":
      return (
        <li className="msg them">
          <span className="who">Them</span>
          <p className={item.final ? "" : "partial"}>{item.text}</p>
        </li>
      );
    case "me":
      return (
        <li className="msg me">
          <span className="who">You said</span>
          <p className={item.final ? "" : "partial"}>“{item.text}”</p>
        </li>
      );
    case "question":
      return (
        <li className="msg answer">
          <QuestionCard q={item.question} s={s} compact={compact} onExpand={() => onExpand(item.question.id)} />
        </li>
      );
  }
}

function QuestionCard({ q, s, compact, onExpand }: { q: QuestionView; s: SessionView; compact: boolean; onExpand: () => void }) {
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const deepStarted = q.deep.text.length > 0 || q.deep.done || q.deepRequested;

  const expand = () => {
    onExpand();
    api.expandAnswer(q.id).catch(() => undefined);
  };
  const rate = (up: boolean) => {
    setRating(up ? "up" : "down");
    api.sendFeedback(q.id, up).catch(() => undefined);
  };

  return (
    <article className="card">
      <header className="card-head">
        <span className="label">Suggested</span>
        <span className="tag">{q.type.replace("_", " ")}</span>
        {q.speculative && <span className="tag">listening…</span>}
      </header>
      <p className="question">{q.text}</p>

      <section className="points">
        <PassHead title="Talking points" text={q.fast.text} />
        {q.fast.text ? <AnswerText text={q.fast.text} /> : !q.fast.done && <p className="muted">Thinking…</p>}
        {q.fast.source === "prepared" && <p className="note">Prepared answer</p>}
        {q.fast.error && q.fast.error !== "cancelled" && <p className="note warn">Talking points failed ({q.fast.error})</p>}
        {q.fast.error === "cancelled" && <p className="note">Stopped for a newer question</p>}
      </section>

      <section className="deep">
        {deepStarted ? (
          <>
            <PassHead title="Detailed answer" text={q.deep.text} />
            {q.deep.text ? <AnswerText text={q.deep.text} /> : !q.deep.done && <p className="muted">Writing…</p>}
            {q.deep.error === "cancelled" && <p className="note">Stopped for a newer question</p>}
            {q.deep.error && q.deep.error !== "cancelled" && <p className="note warn">Detailed answer failed ({q.deep.error})</p>}
          </>
        ) : (
          <button className="link" onClick={expand}>
            Get the detailed answer ▾
          </button>
        )}
        {!deepStarted && !deepStartsAlone(s, q.type) && !compact && <p className="note">Only on request for this question type.</p>}
      </section>

      <footer className="thumbs">
        <button className={rating === "up" ? "on" : ""} onClick={() => rate(true)} title="Helpful">
          👍
        </button>
        <button className={rating === "down" ? "on" : ""} onClick={() => rate(false)} title="Not helpful">
          👎
        </button>
        {!compact && q.fast.refs.length > 0 && (
          <span className="refs muted" title={q.fast.refs.map((r) => r.label).join("\n")}>
            from {q.fast.refs.map((r) => r.label).join(", ")}
          </span>
        )}
      </footer>
    </article>
  );
}

function PassHead({ title, text }: { title: string; text: string }) {
  return (
    <div className="pass-head">
      <span className="pass-title">{title}</span>
      <CopyButton text={text} />
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const c = useMemo(() => copier(setCopied), []);
  useEffect(() => () => c.dispose(), [c]);
  return (
    <button className="copy" disabled={!text} onClick={() => c.copy(text)} title="Copy as plain text">
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function AskBox({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = (value: string | null, respond = false) => {
    setError(null);
    (respond ? api.respond(value) : api.forceQuestion(value)).then(
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
      <button
        type="button"
        disabled={disabled}
        onClick={() => send(text.trim() || null, true)}
        title="Suggest a reply to what was just said: a request, a reminder, a check-in"
      >
        Respond
      </button>
      {error && <span className="note warn">{error}</span>}
    </form>
  );
}
