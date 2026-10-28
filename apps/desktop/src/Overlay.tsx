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
const NO_HOTKEYS: OverlayPrefs["hotkeys"] = {
  toggle_overlay: "",
  click_through: "",
  click_through_off: "",
  opacity_up: "",
  opacity_down: "",
};

function useOverlayPrefs() {
  const [opacity, setOpacity] = useState(1);
  const [compact, setCompactState] = useState(loadCompact);
  const [clickThrough, setClickThrough] = useState(false);
  const [onTop, setOnTopState] = useState(true);
  const [hotkeys, setHotkeys] = useState<OverlayPrefs["hotkeys"]>(NO_HOTKEYS);

  useEffect(() => {
    if (!inTauri) return;
    let live = false;
    const apply = (p: OverlayPrefs) => {
      setOpacity(p.opacity);
      setCompactState(p.compact);
      saveCompact(p.compact);
      setClickThrough(p.click_through);
      setOnTopState(p.on_top);
      setHotkeys(p.hotkeys);
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
  const setOpacityLive = (v: number) => {
    setOpacity(v);
    if (inTauri) api.setOverlayOpacity(v).catch(() => undefined);
  };
  const setOnTop = (on: boolean) => {
    setOnTopState(on);
    if (inTauri) api.setOverlayOnTop(on).catch(() => undefined);
  };
  return { opacity, compact, clickThrough, onTop, hotkeys, setCompact, setOpacityLive, setOnTop };
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
  const { opacity, compact, clickThrough, onTop, hotkeys, setCompact, setOpacityLive, setOnTop } = useOverlayPrefs();
  const shown = useMemo(() => (compact ? items.filter((i) => i.kind === "question") : items), [items, compact]);
  const size = contentSize(shown);
  const body = useRef<HTMLDivElement>(null);
  const follower = useRef(new Follower());
  const [jump, setJump] = useState(false);
  const [menu, setMenu] = useState(false);
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
          Click-through on{hotkeys.click_through_off ? ` — ${hotkeys.click_through_off} to turn off` : " — use your turn-off hotkey"}
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
          title="Drag to resize"
          onMouseDown={(e) => {
            e.preventDefault();
            if (inTauri) getCurrentWindow().startResizeDragging("SouthEast");
          }}
        >
          ⤡
        </button>
        <button className={menu ? "icon on" : "icon"} title="Overlay settings" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
          ⚙
        </button>
        <button className="icon" title="Hide overlay" onClick={() => inTauri && getCurrentWindow().hide()}>
          ×
        </button>
      </header>
      {menu && (
        <OverlayMenu
          opacity={opacity}
          onOpacity={setOpacityLive}
          onTop={onTop}
          onToggleOnTop={setOnTop}
          clickThrough={clickThrough}
          hotkeys={hotkeys}
          onClose={() => setMenu(false)}
        />
      )}

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

const KEY_LABELS: Record<string, string> = {
  CommandOrControl: "Ctrl",
  CommandOrCtrl: "Ctrl",
  CmdOrCtrl: "Ctrl",
  Control: "Ctrl",
  Command: "Cmd",
  Super: "Win",
  Up: "↑",
  Down: "↓",
};

/** A readable chip for an accelerator, e.g. "CommandOrControl+Shift+H" -> "Ctrl+Shift+H". */
function prettyKey(accel: string): string {
  return accel
    .split("+")
    .map((p) => KEY_LABELS[p.trim()] ?? p.trim())
    .join("+");
}

function Hotkey({ accel }: { accel: string }) {
  if (!accel) return null;
  return <kbd className="key">{prettyKey(accel)}</kbd>;
}

/** The overlay's own settings menu (the gear dropdown): quick comfort toggles without opening
 * the main window. "All settings…" brings the main window forward; the rest live here. */
function OverlayMenu({
  opacity,
  onOpacity,
  onTop,
  onToggleOnTop,
  clickThrough,
  hotkeys,
  onClose,
}: {
  opacity: number;
  onOpacity: (v: number) => void;
  onTop: boolean;
  onToggleOnTop: (on: boolean) => void;
  clickThrough: boolean;
  hotkeys: OverlayPrefs["hotkeys"];
  onClose: () => void;
}) {
  return (
    <>
      <div className="menu-backdrop" onClick={onClose} />
      <div className="overlay-menu" role="menu">
        <div className="menu-row col">
          <span className="menu-label">Transparency</span>
          <div className="menu-slider">
            <input
              type="range"
              min={0.3}
              max={1}
              step={0.05}
              value={opacity}
              onChange={(e) => onOpacity(Number(e.target.value))}
            />
            <span className="menu-value">{Math.round(opacity * 100)}%</span>
          </div>
          <span className="menu-hint">
            Step it anytime <Hotkey accel={hotkeys.opacity_up} /> <Hotkey accel={hotkeys.opacity_down} />
          </span>
        </div>

        <button className="menu-row" role="menuitemcheckbox" aria-checked={onTop} onClick={() => onToggleOnTop(!onTop)}>
          <span className="menu-main">
            <span className="menu-label">Keep on top</span>
            <span className="menu-hint">Overlay stays above other apps</span>
          </span>
          <span className={onTop ? "switch on" : "switch"} />
        </button>

        <button
          className="menu-row"
          role="menuitemcheckbox"
          aria-checked={clickThrough}
          title={clickThrough ? "Turn off with the hotkey" : "Clicks pass to the window underneath"}
          onClick={() => inTauri && !clickThrough && api.setClickThrough(true).catch(() => undefined)}
        >
          <span className="menu-main">
            <span className="menu-label">
              Click-through <Hotkey accel={hotkeys.click_through} />
            </span>
            <span className="menu-hint">
              {clickThrough ? `On — ${prettyKey(hotkeys.click_through_off)} to turn off` : "Clicks pass through (top bar stays usable)"}
            </span>
          </span>
          <span className={clickThrough ? "switch on" : "switch"} />
        </button>

        <div className="menu-sep" />

        <button
          className="menu-row link-row"
          onClick={() => {
            onClose();
            if (inTauri) api.openMainSettings().catch(() => undefined);
          }}
        >
          All settings…
        </button>
        <button
          className="menu-row link-row danger"
          onClick={() => inTauri && api.quitApp().catch(() => undefined)}
        >
          Quit Copilot
        </button>
      </div>
    </>
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
