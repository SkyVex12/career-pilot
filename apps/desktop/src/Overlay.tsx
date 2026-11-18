// The always-on-top overlay (docs/design/ui-proposal.md, "Overlay rules"). It is read
// mid-sentence, so:
// - the newest question ("Now") is always in the same place: the top of the answers pane (split
//   and compact), or pinned under the conversation (one column); earlier questions follow in
//   full, newest first. The answers pane never scrolls by itself, so text that streams in grows
//   below what is being read; scrolled down, a new question offers "New answer ↑".
// - each pass says its state in words and an icon (thinking, writing with a caret, done,
//   stopped, failed), and space is held for it, so the first words push nothing.
// - the opacity setting fades the window's background only; all text stays opaque, and the
//   answers keep a backing, so they stay readable over whatever is behind.
// The transcript pane (split) keeps following its newest line. Text size steps with A-/A+ or
// Ctrl+plus/minus/0; compact shows the answers only; hotkeys, click-through, keep on top, copy,
// thumbs, the typed question (Ask) and the reply button (Answer) are as before.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { api, inTauri, onOverlayPrefs, type OverlayPrefs } from "./api";
import { AnswerText } from "./AnswerText";
import { CodeAnswer } from "./CodeAnswer";
import { CodingBar } from "./CodingBar";
import { copier, Follower } from "./follow";
import { prettyKey } from "./hotkeys";
import { GuestNoteBox, GuestNotice, GuestTalk, GuestsButton, NotesLane, VoiceMute } from "./seats";
import { startScreenHost } from "./screen";
import { ScreenChip } from "./screenui";
import { badge, conversation, isScreenQuestion, useSession, type ConversationItem, type PassView, type QuestionView, type SessionView } from "./session";
import { LAYOUT_KEY, TEXT_SCALE_KEY, blockText, loadLayout, loadTextScale, savePref, stepScale, transcriptBlocks, type OverlayLayout, type TranscriptBlock } from "./transcript";
import { Button, ConfirmDialog, IconButton, Kbd, Switch, cx } from "./ui";

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

const NO_HOTKEYS: OverlayPrefs["hotkeys"] = {
  toggle_overlay: "",
  click_through: "",
  click_through_off: "",
  opacity_up: "",
  opacity_down: "",
};

/** The overlay's comfort prefs, kept by the Rust core per monitor: opacity and view, and whether
 * clicks pass through. The core pushes changes (a hotkey, a move to another monitor); the overlay
 * asks once on mount. `compact` keeps a localStorage copy so the first paint matches. */
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

/// The step Ctrl plus a key asks of the text size: +1, -1, 0 for the default, or null.
export function textKeyStep(e: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean }): number | null {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return null;
  if (e.key === "=" || e.key === "+") return 1;
  if (e.key === "-" || e.key === "_") return -1;
  if (e.key === "0") return 0;
  return null;
}

const STATUS_ICON: Record<string, string> = { ok: "●", degraded: "▲", reconnecting: "◌", off: "○" };

export function StatusBadge({ s }: { s: SessionView }) {
  const b = badge(s);
  return (
    <span className={`ov-status ov-status-${b.state}`} role="status" title={b.text} data-tauri-drag-region>
      <span className="ov-status-icon" aria-hidden="true">
        {STATUS_ICON[b.state]}
      </span>
      <span className="ov-status-text">{b.text}</span>
    </span>
  );
}

export default function Overlay() {
  const [s, dispatch] = useSession();
  const { opacity, compact, clickThrough, onTop, hotkeys, setCompact, setOpacityLive, setOnTop } = useOverlayPrefs();
  const view = useViewPrefs();
  const stepText = view.stepText;
  // Sharing the screen runs in the window where the host clicked (screen.ts).
  useEffect(() => (inTauri ? startScreenHost() : undefined), []);

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

  const offKey = prettyKey(hotkeys.click_through_off || "CommandOrControl+Shift+G");
  // Watching someone else's call (a guest seat): read only, nothing acts on the call.
  const guest = s.seat !== null;
  const host = s.seat?.hostName ?? "";
  return (
    <div className={cx("ov", compact && "is-compact")} style={{ "--ov-alpha": opacity } as CSSProperties}>
      <header className="ov-head" data-tauri-drag-region>
        <StatusBadge s={s} />
        {!guest && <GuestsButton s={s} />}
        {!guest && <VoiceMute s={s} />}
        {!guest && <ScreenChip s={s} />}
        <span className="ov-speaking" data-tauri-drag-region>
          {s.speaking.them && <span className="ov-pill">them</span>}
          {s.speaking.me && <span className="ov-pill ov-pill-me">you</span>}
        </span>
        <span className="ov-spacer" data-tauri-drag-region />
        <ViewControls view={view} compact={compact} onCompact={() => setCompact(!compact)} />
        <IconButton
          label="Drag to resize"
          icon="⤡"
          size="sm"
          onMouseDown={(e) => {
            e.preventDefault();
            if (inTauri) getCurrentWindow().startResizeDragging("SouthEast");
          }}
        />
        <OverlayMenu
          opacity={opacity}
          onOpacity={setOpacityLive}
          onTop={onTop}
          onToggleOnTop={setOnTop}
          clickThrough={clickThrough}
          hotkeys={hotkeys}
          running={s.running}
        />
        <IconButton label="Minimize overlay" icon="–" size="sm" onClick={() => inTauri && void api.minimizeOverlay().catch(() => undefined)} />
        <IconButton label="Hide overlay" icon="✕" size="sm" onClick={() => inTauri && getCurrentWindow().hide()} />
      </header>
      {clickThrough && (
        <div className="ov-clickthrough" role="status">
          Click-through on · <Kbd>{offKey}</Kbd> turns it off
        </div>
      )}
      <GuestNotice s={s} />

      <ConversationView
        s={s}
        compact={compact}
        layout={view.layout}
        textScale={view.textScale}
        live
        readOnly={guest}
        meLabel={guest ? host : undefined}
        onExpand={(id) => dispatch({ type: "deep_requested", id })}
        emptyAnswers={
          guest
            ? `Watching ${host}'s call. Answers appear here as ${host} gets them.`
            : s.running
              ? "Listening. Press the hotkey to answer the last thing said."
              : "No call running. Start one in the Copilot window."
        }
        emptyTranscript={s.running ? "Nothing heard yet." : "No call."}
        showMe={guest}
      />

      {guest ? (
        <>
          <GuestTalk s={s} />
          <GuestNoteBox s={s} dispatch={dispatch} />
        </>
      ) : (
        <>
          <NotesLane s={s} dispatch={dispatch} />
          <CodingBar running={s.running} />
          <AskBox disabled={!s.running} />
        </>
      )}
      <span className="ov-grip" aria-hidden="true" onMouseDown={() => inTauri && getCurrentWindow().startResizeDragging("SouthEast")} />
    </div>
  );
}

/** The header's view buttons: text size, split or one column, and compact. */
export function ViewControls({ view, compact, onCompact }: { view: ViewPrefs; compact: boolean; onCompact: () => void }) {
  const split = view.layout === "split";
  return (
    <span className="ov-view">
      <IconButton label="Smaller text (Ctrl+minus)" icon="A−" size="sm" className="ov-text-size" onClick={() => view.stepText(-1)} />
      <IconButton label={`Larger text (Ctrl+plus), now ${Math.round(view.textScale * 100)}%`} icon="A+" size="sm" className="ov-text-size" onClick={() => view.stepText(1)} />
      {!compact && (
        <IconButton
          label={split ? "One column: transcript and answers together" : "Split: transcript left, answers right"}
          icon={split ? "▤" : "◫"}
          size="sm"
          onClick={() => view.setLayout(split ? "stacked" : "split")}
        />
      )}
      <IconButton label={compact ? "Leave compact view" : "Compact: answers only"} icon={compact ? "☰" : "▭"} size="sm" pressed={compact} onClick={onCompact} />
    </span>
  );
}

/** The conversation in the chosen layout. Live (the overlay), the newest question comes first;
 * a replay (History) reads top to bottom, in the order the call went, and passes `ratings`. */
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
  readOnly = false,
  meLabel,
}: {
  /** Watching someone else's call (a guest seat): nothing on a card acts on the call. */
  readOnly?: boolean;
  /** Whose the "me" lines are when they are not the reader's (a guest sees the host's). */
  meLabel?: string;
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
  const questions = useMemo(() => items.filter((i): i is Extract<ConversationItem, { kind: "question" }> => i.kind === "question").map((i) => i.question), [items]);
  const blocks = useMemo(() => transcriptBlocks(s, { showMe }), [s, showMe]);
  const answersPane = useRef<HTMLDivElement>(null);
  const scale = { "--ov-scale": textScale } as CSSProperties;
  const nowId = live ? (questions[questions.length - 1]?.id ?? null) : null;
  const card = (q: QuestionView) => (
    <QuestionCard key={q.id} q={q} now={q.id === nowId} compact={compact} onExpand={() => onExpand?.(q.id)} past={ratings ? { rating: ratings[q.id] ?? null } : null} readOnly={readOnly} />
  );
  const answersList = (list: QuestionView[]) =>
    list.length === 0 ? (
      <p className="ov-empty">{emptyAnswers}</p>
    ) : (
      <ol className="ov-cards" aria-label="Suggestions">
        {list.map((q) => (
          <li key={q.id}>{card(q)}</li>
        ))}
      </ol>
    );
  // Live: newest first. A replay: in the order they came.
  const ordered = live ? [...questions].reverse() : questions;

  if (compact) {
    return (
      <div className="ov-body ov-single" style={scale}>
        <AnswersPane paneRef={answersPane} newestId={nowId}>
          {answersList(ordered)}
        </AnswersPane>
      </div>
    );
  }

  if (layout === "stacked") {
    // The conversation in order; live, the current question is pinned under it.
    const shown = live ? items.filter((i) => !(i.kind === "question" && i.question.id === nowId)) : items;
    const now = live ? questions.find((q) => q.id === nowId) : undefined;
    return (
      <div className="ov-body ov-stacked" style={scale}>
        <ScrollPane className="ov-pane-flow" size={contentSize(shown)} live={live} label="Conversation">
          {shown.length === 0 && !now ? <p className="ov-empty">{emptyAnswers}</p> : <Flow items={shown} card={card} meLabel={meLabel} />}
        </ScrollPane>
        {now && (
          <section className="ov-now-pinned" aria-label="Current suggestion">
            {card(now)}
          </section>
        )}
      </div>
    );
  }

  const toCard = (id: string) => {
    const el = answersPane.current?.querySelector(`[data-q="${id}"]`);
    el?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const transcriptSize = `${blocks.length}:${blocks.reduce((n, b) => n + blockText(b).length, 0)}`;
  return (
    <div className="ov-body ov-split" style={scale}>
      <ScrollPane className="ov-pane-transcript" title="Transcript" size={transcriptSize} live={live} label="Transcript">
        {blocks.length === 0 ? <p className="ov-empty">{emptyTranscript}</p> : <Transcript blocks={blocks} onAsked={toCard} meLabel={meLabel} />}
      </ScrollPane>
      <AnswersPane title="Suggestions" paneRef={answersPane} newestId={nowId}>
        {answersList(ordered)}
      </AnswersPane>
    </div>
  );
}

/// Changes whenever anything shown grows, so a following pane can follow it.
function contentSize(items: ConversationItem[]): string {
  let chars = 0;
  for (const it of items) {
    if (it.kind === "question") chars += it.question.fast.text.length + it.question.deep.text.length + (it.question.deep.done ? 1 : 0);
    else chars += it.text.length;
  }
  return `${items.length}:${chars}`;
}

/** The suggestions: anchored to the top, never scrolled by itself. Scrolled down when a newer
 * question arrives, the browser keeps what is being read in place and "New answer ↑" goes up. */
function AnswersPane({ title, paneRef, newestId, children }: { title?: string; paneRef: RefObject<HTMLDivElement | null>; newestId: string | null; children: ReactNode }) {
  const [unseen, setUnseen] = useState(false);
  const last = useRef(newestId);
  useEffect(() => {
    if (newestId === last.current) return;
    last.current = newestId;
    const el = paneRef.current;
    if (el && el.scrollTop > 24) setUnseen(true);
  }, [newestId, paneRef]);
  const toTop = () => {
    paneRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    setUnseen(false);
  };
  return (
    <section className="ov-pane ov-pane-answers" aria-label={title ?? "Suggestions"}>
      {title && <h2 className="ov-pane-head">{title}</h2>}
      <div
        className="ov-pane-body"
        ref={paneRef}
        onScroll={(e) => {
          if (e.currentTarget.scrollTop <= 24) setUnseen(false);
        }}
      >
        {children}
      </div>
      {unseen && (
        <button type="button" className="ov-jump ov-jump-top" onClick={toTop}>
          New answer ↑
        </button>
      )}
    </section>
  );
}

/** A pane that follows its newest content while the reader is at the bottom (the transcript). */
function ScrollPane({ className, title, size, live, label, children }: { className: string; title?: string; size: string; live: boolean; label: string; children: ReactNode }) {
  const body = useRef<HTMLDivElement>(null);
  const follower = useRef(new Follower());
  const [jump, setJump] = useState(false);

  useLayoutEffect(() => {
    const el = body.current;
    if (!el || !live) return;
    follower.current.onContent(el);
    setJump(follower.current.showJump);
  }, [size, live]);

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
    <section className={`ov-pane ${className}`} aria-label={label}>
      {title && <h2 className="ov-pane-head">{title}</h2>}
      <div className="ov-pane-body ov-follow" ref={body} onScroll={onScroll}>
        {children}
      </div>
      {jump && (
        <button type="button" className="ov-jump" onClick={toLatest}>
          Jump to latest ↓
        </button>
      )}
    </section>
  );
}

/** The other side's words as joined blocks; a block that got a suggestion links to its card. */
function Transcript({ blocks, onAsked, meLabel }: { blocks: TranscriptBlock[]; onAsked: (id: string) => void; meLabel?: string }) {
  return (
    <ol className="tx">
      {blocks.map((b) => (
        <li key={b.key} className={`line ${b.who}`}>
          <span className="who">{b.who === "me" ? (meLabel ?? "You") : "Them"}</span>
          <p>
            {b.parts.map((p, i) => (
              <span key={p.key} className={p.final ? undefined : "partial"}>
                {i > 0 ? " " : ""}
                {p.text}
              </span>
            ))}
          </p>
          {b.asked.length > 0 && (
            <button type="button" className="asked" onClick={() => onAsked(b.asked[b.asked.length - 1])}>
              Suggestion →
            </button>
          )}
        </li>
      ))}
    </ol>
  );
}

/** One column: lines and questions in the order they came. */
function Flow({ items, card, meLabel }: { items: ConversationItem[]; card: (q: QuestionView) => ReactNode; meLabel?: string }) {
  return (
    <ol className="ov-flow">
      {items.map((it) => {
        if (it.kind === "question") return <li key={it.key}>{card(it.question)}</li>;
        return (
          <li key={it.key} className={`msg ${it.kind}`}>
            <span className="who">{it.kind === "me" ? (meLabel ?? "You said") : "Them"}</span>
            <p className={it.final ? "" : "partial"}>{it.kind === "me" ? `“${it.text}”` : it.text}</p>
          </li>
        );
      })}
    </ol>
  );
}

/** The overlay's settings (the gear): opacity, keep on top, click-through, all settings, quit.
 * Esc or a click outside closes it and gives focus back to the gear. */
function OverlayMenu({
  opacity,
  onOpacity,
  onTop,
  onToggleOnTop,
  clickThrough,
  hotkeys,
  running,
}: {
  opacity: number;
  onOpacity: (v: number) => void;
  onTop: boolean;
  onToggleOnTop: (on: boolean) => void;
  clickThrough: boolean;
  hotkeys: OverlayPrefs["hotkeys"];
  running: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [quitting, setQuitting] = useState(false);
  const gear = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => {
    setOpen(false);
    gear.current?.focus();
  }, []);
  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector<HTMLElement>("input, button")?.focus();
    const onDown = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !gear.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  const quit = () => {
    if (inTauri) api.quitApp().catch(() => undefined);
  };
  return (
    <span className="ov-menu-wrap">
      <IconButton ref={gear} label="Overlay settings" icon="⚙" size="sm" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)} />
      {open && (
        <div
          ref={panel}
          className="ov-menu"
          role="dialog"
          aria-label="Overlay settings"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              close();
            }
          }}
        >
          <div className="ov-menu-row">
            <label className="ov-menu-label" htmlFor="ov-opacity">
              Opacity <span className="ov-menu-value">{Math.round(opacity * 100)}%</span>
            </label>
            <input
              id="ov-opacity"
              type="range"
              min={0.3}
              max={1}
              step={0.05}
              value={opacity}
              aria-valuetext={`${Math.round(opacity * 100)}%`}
              onChange={(e) => onOpacity(Number(e.target.value))}
            />
            <span className="ov-menu-hint">
              The background fades; text stays solid. Step it anytime: {hotkeys.opacity_up && <Kbd>{prettyKey(hotkeys.opacity_up)}</Kbd>}{" "}
              {hotkeys.opacity_down && <Kbd>{prettyKey(hotkeys.opacity_down)}</Kbd>}
            </span>
          </div>
          <div className="ov-menu-row">
            <Switch label="Keep on top" hint="Above other apps" checked={onTop} onChange={onToggleOnTop} />
          </div>
          <div className="ov-menu-row">
            <Switch
              label="Click-through"
              hint={clickThrough ? `On: ${prettyKey(hotkeys.click_through_off || "CommandOrControl+Shift+G")} turns it off` : `Clicks pass through; the top bar stays usable.${hotkeys.click_through ? ` Hotkey ${prettyKey(hotkeys.click_through)}` : ""}`}
              checked={clickThrough}
              onChange={(on) => {
                // Turned off by its hotkey only: the mouse cannot reach the overlay while it is on.
                if (on && inTauri) api.setClickThrough(true).catch(() => undefined);
              }}
            />
          </div>
          <div className="ov-menu-sep" />
          <Button
            variant="ghost"
            size="sm"
            className="ov-menu-item"
            onClick={() => {
              close();
              if (inTauri) api.openMainSettings().catch(() => undefined);
            }}
          >
            All settings…
          </Button>
          <div className="ov-menu-sep" />
          <Button variant="danger-ghost" size="sm" className="ov-menu-item" onClick={() => (running ? setQuitting(true) : quit())}>
            Quit Copilot{running ? "…" : ""}
          </Button>
        </div>
      )}
      <ConfirmDialog open={quitting} onOpenChange={setQuitting} title="Quit during a call?" confirmLabel="Quit Copilot" onConfirm={quit}>
        The call stops here. It can be opened again from History while it is still running.
      </ConfirmDialog>
    </span>
  );
}

type PassState = { icon: string; text: string; tone: "wait" | "live" | "done" | "warn" };

/** What a pass is doing, in words. */
export function passState(p: PassView, kind: "fast" | "deep"): PassState {
  if (p.error === "cancelled") return { icon: "⏹", text: "Stopped for a newer question", tone: "warn" };
  if (p.error) return { icon: "⚠", text: `Failed (${p.error})`, tone: "warn" };
  if (p.done) return { icon: "✓", text: p.source === "prepared" ? "Done · prepared" : "Done", tone: "done" };
  if (p.text) return { icon: "◌", text: "Writing", tone: "live" };
  return { icon: "◌", text: kind === "fast" ? "Thinking" : "Queued", tone: "wait" };
}

function QuestionCard({
  q,
  now,
  compact,
  onExpand,
  past,
  readOnly = false,
}: {
  /** A guest's view: no expand, no thumbs; the host's choices show as they come. */
  readOnly?: boolean;
  q: QuestionView;
  /** The newest question: the largest type, in the fixed place. */
  now: boolean;
  compact: boolean;
  onExpand: () => void;
  /** A past call (History): nothing can be asked or rated any more. */
  past: { rating: "up" | "down" | null } | null;
}) {
  const [rating, setRating] = useState<"up" | "down" | null>(past?.rating ?? null);
  // A screenshot's question (coding mode): its answer and its code, as code and Markdown.
  const screen = isScreenQuestion(q);
  const Text = screen ? CodeAnswer : AnswerText;
  // A screen's code pass runs only when a task needs code or an exact output: its section
  // shows while the reading says one is coming, and with whatever it wrote.
  const codeComing = q.screen?.tasks.some((t) => SECOND_PASS_KINDS.has(t.kind)) ?? false;
  const deepStarted = screen
    ? q.deep.text.length > 0 || q.deep.error !== null || (codeComing && !q.deep.done)
    : q.deep.text.length > 0 || q.deep.done || q.deepRequested;
  const fast = passState(q.fast, "fast");
  const deep = passState(q.deep, "deep");
  // Space stays held for the talking points until the detailed answer is done, so nothing
  // under them jumps up while the reader is still on them.
  const holdFast = !q.fast.done || (deepStarted && !q.deep.done);

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
    <article className={cx("card", now ? "is-now" : "is-earlier", q.speculative && "is-speculative")} data-q={q.id} aria-label={`${now ? "Now: " : ""}${q.text}`}>
      <p className="question">
        {q.text}
        {q.speculative && <span className="ui-visually-hidden"> (still listening)</span>}
      </p>
      {q.screen && <ScreenTasks reading={q.screen} />}

      <div role="group" className={cx("points pass pass-fast", holdFast && "is-held", screen && "is-code")} aria-label={screen ? "Answer" : "Talking points"}>
        {q.fast.text && <Text text={q.fast.text} streaming={!q.fast.done} />}
        <PassStatus state={fast} />
      </div>

      {deepStarted ? (
        <div role="group" className={cx("deep pass pass-deep", screen && "is-code")} aria-label={screen ? "Code" : "Detailed answer"}>
          <span className="pass-title" aria-hidden="true">
            {screen ? "Code" : "Detailed"}
          </span>
          {q.deep.text ? (
            <Text text={q.deep.text} streaming={!q.deep.done} />
          ) : (
            !q.deep.done && (
              <div className="ov-reserve" aria-hidden="true">
                <span />
                <span />
              </div>
            )
          )}
          <PassStatus state={deep} />
        </div>
      ) : (
        !past &&
        !readOnly &&
        !screen && (
          <button type="button" className="ov-get-deep" onClick={expand}>
            Get the detailed answer ▾
          </button>
        )
      )}

      <footer className="card-tools">
        {!screen && <CopyButton text={q.fast.text} label="Copy talking points" short="Copy" />}
        {!screen && deepStarted && <CopyButton text={q.deep.text} label="Copy detailed answer" short="Copy detailed" />}
        {!readOnly && (
          <>
            <button type="button" className="ov-thumb" aria-pressed={rating === "up"} aria-label="Helpful" title="Helpful" disabled={Boolean(past)} onClick={() => rate(true)}>
              👍
            </button>
            <button type="button" className="ov-thumb" aria-pressed={rating === "down"} aria-label="Not helpful" title="Not helpful" disabled={Boolean(past)} onClick={() => rate(false)}>
              👎
            </button>
          </>
        )}
        {!compact && q.fast.refs.length > 0 && (
          <span className="refs" title={q.fast.refs.map((r) => r.label).join("\n")}>
            from {q.fast.refs.map((r) => r.label).join(", ")}
          </span>
        )}
      </footer>
    </article>
  );
}

/** The task kinds a screen's second pass answers (copilot_core.screen DEEP_KINDS). */
const SECOND_PASS_KINDS = new Set(["code", "fix", "complete", "output"]);

const KIND_LABELS: Record<string, string> = {
  code: "Code", choice: "Choice", short: "Short answer", output: "Output", fix: "Fix the bug",
  sql: "SQL", complete: "Complete", design: "Design",
};

/** What the screen holds, in screen order, and the language it is in. */
function ScreenTasks({ reading }: { reading: NonNullable<QuestionView["screen"]> }) {
  if (reading.tasks.length === 0) {
    return <p className="screen-tasks is-unreadable">Nothing to answer on this shot{reading.unreadable ? `: ${reading.unreadable}` : ""}. Take another.</p>;
  }
  return (
    <ul className="screen-tasks" aria-label="Tasks on the screen">
      {reading.tasks.map((t) => (
        <li key={t.n} className="screen-task">
          {reading.tasks.length > 1 && <span className="screen-task-n">{t.n}</span>}
          {KIND_LABELS[t.kind] ?? t.kind}
          {t.language && <span className="screen-task-lang"> · {t.language}</span>}
        </li>
      ))}
    </ul>
  );
}

/** A pass's state. Writing shows as the caret and done as nothing, so only waiting, stopped
 * and failed are drawn; a screen reader hears every state. */
function PassStatus({ state }: { state: PassState }) {
  const shown = state.tone === "wait" || state.tone === "warn";
  return (
    <p className={cx("pass-status", `pass-state-${state.tone}`, !shown && "ui-visually-hidden")}>
      {shown && <span aria-hidden="true">{state.icon} </span>}
      {state.text}
    </p>
  );
}

function CopyButton({ text, label, short }: { text: string; label: string; short: string }) {
  const [copied, setCopied] = useState(false);
  const c = useMemo(() => copier(setCopied), []);
  useEffect(() => () => c.dispose(), [c]);
  return (
    <button type="button" className="copy" disabled={!text} onClick={() => c.copy(text)} aria-label={label} title="Copy as plain text">
      {copied ? "Copied" : short}
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
      className="ov-ask"
      onSubmit={(e) => {
        e.preventDefault();
        send(text.trim() || null);
      }}
    >
      <input
        className="ov-ask-input"
        aria-label="Question"
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        placeholder={disabled ? "No call running" : "Type a question, or Enter to answer what was just said"}
      />
      <button type="submit" className="ov-ask-btn" disabled={disabled}>
        Ask
      </button>
      <button type="button" className="ov-ask-btn" disabled={disabled} onClick={() => send(text.trim() || null, true)} title="Suggest a reply to what was just said: a request, a reminder, a check-in">
        Answer
      </button>
      {error && (
        <span className="ov-ask-error" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}
