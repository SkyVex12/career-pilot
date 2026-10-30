// The always-on-top overlay. Two layouts, switched from the header and remembered:
// - split (the default): the transcript in the left third (the other side live; both sides
//   in History's replay), each side's short
//   lines joined into blocks (transcript.ts), and the suggested answers in the right two
//   thirds. Each pane scrolls on its own and follows its newest content while the reader is at
//   its bottom; scrolled up, it stays put and offers "Jump to latest". A narrow window stacks
//   the panes.
// - one column: the whole conversation in order, the other side on the left, the answers on
//   the right, and in History the user's own words ("You said").
// Compact shows the answers only, tighter, in either layout. Compact shows the answers only.
// Text size steps with the A-/A+ buttons or Ctrl+plus/minus/0. Hotkey, typed question, thumbs
// and the status badge as before.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { api, inTauri, onOverlayPrefs, type OverlayPrefs } from "./api";
import { AnswerText } from "./AnswerText";
import { copier, Follower } from "./follow";
import { badge, conversation, deepStartsAlone, useSession, type ConversationItem, type QuestionView, type SessionView } from "./session";
import { LAYOUT_KEY, TEXT_SCALE_KEY, blockText, loadLayout, loadTextScale, savePref, stepScale, transcriptBlocks, type OverlayLayout, type TranscriptBlock } from "./transcript";

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

/** Text size and layout, shared by the overlay and History's replay (the same localStorage),
 * and kept in step between the two windows. */
export function useViewPrefs() {
  const [textScale, setScale] = useState(loadTextScale);
  const [layout, setLayoutState] = useState<OverlayLayout>(loadLayout);
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === TEXT_SCALE_KEY) setScale(loadTextScale());
      if (e.key === LAYOUT_KEY) setLayoutState(loadLayout());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  const stepText = useCallback((steps: number) => {
    setScale((v) => {
      const next = steps === 0 ? 1 : stepScale(v, steps);
      savePref(TEXT_SCALE_KEY, String(next));
      return next;
    });
  }, []);
  const setLayout = useCallback((l: OverlayLayout) => {
    setLayoutState(l);
    savePref(LAYOUT_KEY, l);
  }, []);
  return { textScale, layout, stepText, setLayout };
}

export type ViewPrefs = ReturnType<typeof useViewPrefs>;

/// Changes whenever anything shown grows, so the view can follow it.
function contentSize(items: ConversationItem[]): string {
  let chars = 0;
  for (const it of items) {
    if (it.kind === "question") chars += it.question.fast.text.length + it.question.deep.text.length + (it.question.deep.done ? 1 : 0);
    else chars += it.text.length;
  }
  return `${items.length}:${chars}`;
}

/// The step Ctrl plus a key asks of the text size: +1, -1, 0 for the default, or null.
export function textKeyStep(e: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean }): number | null {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return null;
  if (e.key === "=" || e.key === "+") return 1;
  if (e.key === "-" || e.key === "_") return -1;
  if (e.key === "0") return 0;
  return null;
}

export default function Overlay() {
  const [s, dispatch] = useSession();
  const { opacity, compact, clickThrough, onTop, hotkeys, setCompact, setOpacityLive, setOnTop } = useOverlayPrefs();
  const view = useViewPrefs();
  const [menu, setMenu] = useState(false);
  const b = badge(s);
  const stepText = view.stepText;

  // Ctrl+plus / Ctrl+minus / Ctrl+0 size the text while the overlay has focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const steps = textKeyStep(e);
      if (steps === null) return;
      e.preventDefault();
      stepText(steps);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stepText]);

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
        <ViewControls view={view} compact={compact} onCompact={() => setCompact(!compact)} />
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

      <ConversationView
        s={s}
        compact={compact}
        layout={view.layout}
        textScale={view.textScale}
        live
        onExpand={(id) => dispatch({ type: "deep_requested", id })}
        emptyAnswers={s.running ? "Listening. Press the hotkey to answer the last thing said." : "Start a session in the main window."}
        emptyTranscript={s.running ? "Nothing heard yet." : "No session."}
        showMe={false}
      />

      <AskBox disabled={!s.running} />
      <span className="grip" title="Resize" onMouseDown={() => inTauri && getCurrentWindow().startResizeDragging("SouthEast")} />
    </div>
  );
}

/** The header's view buttons: text size, split or one column, and compact. */
export function ViewControls({ view, compact, onCompact }: { view: ViewPrefs; compact: boolean; onCompact: () => void }) {
  const split = view.layout === "split";
  return (
    <>
      <button className="icon text-size" title="Smaller text (Ctrl+minus)" onClick={() => view.stepText(-1)}>
        A−
      </button>
      <button className="icon text-size" title={`Larger text (Ctrl+plus), now ${Math.round(view.textScale * 100)}%`} onClick={() => view.stepText(1)}>
        A+
      </button>
      <button
        className="icon"
        title={split ? "One column: transcript and answers together" : "Split: transcript left, answers right"}
        aria-pressed={split}
        onClick={() => view.setLayout(split ? "stacked" : "split")}
      >
        {split ? "▤" : "◫"}
      </button>
      <button
        className="icon"
        title={compact ? "Leave compact view" : "Compact: answers only"}
        aria-pressed={compact}
        onClick={onCompact}
      >
        {compact ? "☰" : "▭"}
      </button>
    </>
  );
}

/** The conversation in the chosen layout. `live` follows new content; a replay (History)
 * starts at the top and passes `ratings`. */
export function ConversationView({
  s,
  compact,
  layout,
  textScale,
  live,
  onExpand,
  ratings,
  emptyAnswers,
  emptyTranscript,
  showMe,
}: {
  /** The user's own words: shown in History, left out live (the user knows what they said). */
  showMe: boolean;
  s: SessionView;
  compact: boolean;
  layout: OverlayLayout;
  textScale: number;
  live: boolean;
  onExpand?: (id: string) => void;
  ratings?: Record<string, "up" | "down" | null>;
  emptyAnswers: string;
  emptyTranscript: string;
}) {
  const items = useMemo(() => conversation(s).filter((i) => showMe || i.kind !== "me"), [s, showMe]);
  const answers = useMemo(() => items.filter((i) => i.kind === "question"), [items]);
  const blocks = useMemo(() => transcriptBlocks(s, { showMe }), [s, showMe]);
  const answersPane = useRef<HTMLDivElement>(null);
  const scale = { "--text-scale": textScale } as CSSProperties;

  // Compact is the answers only, whichever the layout.
  if (compact || layout === "stacked") {
    const shown = compact ? answers : items;
    return (
      <div className="overlay-single" style={scale}>
        <ScrollPane className="pane-single" size={contentSize(shown)} live={live}>
          {shown.length === 0 ? <p className="muted empty">{emptyAnswers}</p> : <Conversation items={shown} s={s} compact={compact} onExpand={onExpand} ratings={ratings} />}
        </ScrollPane>
      </div>
    );
  }

  const toCard = (id: string) => {
    const el = answersPane.current?.querySelector(`[data-q="${id}"]`);
    el?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const transcriptSize = `${blocks.length}:${blocks.reduce((n, b) => n + blockText(b).length, 0)}`;
  return (
    <div className="overlay-split" style={scale}>
      <ScrollPane className="pane-transcript" title="Transcript" size={transcriptSize} live={live}>
        {blocks.length === 0 ? <p className="muted empty">{emptyTranscript}</p> : <Transcript blocks={blocks} onAsked={toCard} />}
      </ScrollPane>
      <ScrollPane className="pane-answers" title="Suggestions" size={contentSize(answers)} live={live} paneRef={answersPane}>
        {answers.length === 0 ? <p className="muted empty">{emptyAnswers}</p> : <Conversation items={answers} s={s} compact={false} onExpand={onExpand} ratings={ratings} />}
      </ScrollPane>
    </div>
  );
}

/** One scrolling pane that follows its newest content while the reader is at the bottom. */
function ScrollPane({
  className,
  title,
  size,
  live,
  paneRef,
  children,
}: {
  className: string;
  title?: string;
  size: string;
  live: boolean;
  paneRef?: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  const own = useRef<HTMLDivElement>(null);
  const body = paneRef ?? own;
  const follower = useRef(new Follower());
  const [jump, setJump] = useState(false);

  useLayoutEffect(() => {
    const el = body.current;
    if (!el || !live) return;
    follower.current.onContent(el);
    setJump(follower.current.showJump);
  }, [size, live, body]);

  const onScroll = () => {
    const el = body.current;
    if (!el || !live) return;
    follower.current.onScroll(el);
    setJump(follower.current.showJump);
  };
  const toLatest = () => {
    const el = body.current;
    if (!el) return;
    follower.current.jump(el);
    setJump(false);
  };
  return (
    <section className={`pane ${className}`}>
      {title && <h2 className="pane-head">{title}</h2>}
      <div className="pane-body" ref={body} onScroll={onScroll}>
        <div className="pane-content">{children}</div>
      </div>
      {jump && (
        <button className="jump" onClick={toLatest}>
          Jump to latest ↓
        </button>
      )}
    </section>
  );
}

/** Both sides of the call as joined blocks; a block that got a suggestion links to its card. */
function Transcript({ blocks, onAsked }: { blocks: TranscriptBlock[]; onAsked: (id: string) => void }) {
  return (
    <ol className="tx">
      {blocks.map((b) => (
        <li key={b.key} className={`line ${b.who}`}>
          <span className="who">{b.who === "me" ? "You" : "Them"}</span>
          <p>
            {b.parts.map((p, i) => (
              <span key={p.key} className={p.final ? undefined : "partial"}>
                {i > 0 ? " " : ""}
                {p.text}
              </span>
            ))}
          </p>
          {b.asked.length > 0 && (
            <button className="asked" title="Show the suggestion for this" onClick={() => onAsked(b.asked[b.asked.length - 1])}>
              Suggestion →
            </button>
          )}
        </li>
      ))}
    </ol>
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

/** The conversation list, as the overlay shows it. History passes `ratings` for a past call:
 * read only, with the thumbs the user gave during the call. */
export function Conversation({
  items,
  s,
  compact,
  onExpand,
  ratings,
}: {
  items: ConversationItem[];
  s: SessionView;
  compact: boolean;
  onExpand?: (id: string) => void;
  ratings?: Record<string, "up" | "down" | null>;
}) {
  return (
    <ol className="conversation">
      {items.map((it) => (
        <Item key={it.key} item={it} s={s} compact={compact} onExpand={onExpand} ratings={ratings} />
      ))}
    </ol>
  );
}

function Item({
  item,
  s,
  compact,
  onExpand,
  ratings,
}: {
  item: ConversationItem;
  s: SessionView;
  compact: boolean;
  onExpand?: (id: string) => void;
  ratings?: Record<string, "up" | "down" | null>;
}) {
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
        <li className="msg answer" data-q={item.question.id}>
          <QuestionCard
            q={item.question}
            s={s}
            compact={compact}
            onExpand={() => onExpand?.(item.question.id)}
            past={ratings ? { rating: ratings[item.question.id] ?? null } : null}
          />
        </li>
      );
  }
}

function QuestionCard({
  q,
  s,
  compact,
  onExpand,
  past,
}: {
  q: QuestionView;
  s: SessionView;
  compact: boolean;
  onExpand: () => void;
  /** A past call (History): nothing can be asked or rated any more. */
  past: { rating: "up" | "down" | null } | null;
}) {
  const [rating, setRating] = useState<"up" | "down" | null>(past?.rating ?? null);
  const deepStarted = q.deep.text.length > 0 || q.deep.done || q.deepRequested;

  const expand = () => {
    onExpand();
    api.expandAnswer(q.id).catch(() => undefined);
  };
  const rate = (up: boolean) => {
    if (past) return;
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
        ) : past ? (
          !compact && <p className="note">No detailed answer was asked for.</p>
        ) : (
          <button className="link" onClick={expand}>
            Get the detailed answer ▾
          </button>
        )}
        {!past && !deepStarted && !deepStartsAlone(s, q.type) && !compact && <p className="note">Only on request for this question type.</p>}
      </section>

      <footer className="thumbs">
        <button className={rating === "up" ? "on" : ""} disabled={Boolean(past)} onClick={() => rate(true)} title="Helpful">
          👍
        </button>
        <button className={rating === "down" ? "on" : ""} disabled={Boolean(past)} onClick={() => rate(false)} title="Not helpful">
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
