// Streamed answer text: bullet lines as a list, citation ids like [n:3f2a] left out (they are
// for the server and History's sources, not for reading mid-call; a half-written one is held
// back while it streams), and suggested details (a line ending in [~]: probably true of the
// user's work, not in their data) dimmed with a "check" tag, so the user sees what to verify
// before saying it. While a pass is still writing, a caret follows the last word; a bullet
// appears only with its first word.
const CITE = /[ \t]*\[n:[0-9a-z]+\]/gi;
/** The end of a streaming line that may be a citation still being written: "[", "[n", "[n:3f". */
const CITE_PARTIAL = /[ \t]*\[(n(:[0-9a-z]*)?)?$/i;
const SUGGESTED = /\s*\[~\]/g;
const BULLET = /^\s*[-*•]\s+/;
/** A line that is only a bullet's marker so far ("-" or "- "), still waiting for its words. */
const MARKER_ONLY = /^\s*[-*•]\s*$/;

export function isSuggested(text: string): boolean {
  return /\[~\]/.test(text);
}

/** The lines worth drawing: no blank lines, and no bullet that has no words yet. */
export function answerLines(text: string): string[] {
  return text.split("\n").filter((l) => l.trim() && !MARKER_ONLY.test(l));
}

/** A line as it is read: citation ids out; while streaming, a trailing half-written one too. */
export function readableLine(text: string, streaming = false): string {
  const out = text.replace(CITE, "");
  return streaming ? out.replace(CITE_PARTIAL, "") : out;
}

function Line({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const suggested = isSuggested(text);
  return (
    <span className={suggested ? "suggested" : undefined}>
      <span>{readableLine(text.replace(SUGGESTED, ""), streaming)}</span>
      {suggested && (
        <span className="suggest-tag" title="A suggestion, not from your data: check it before you say it">
          check
        </span>
      )}
    </span>
  );
}

const Caret = () => <span className="ov-caret" aria-hidden="true" />;

export function AnswerText({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const lines = answerLines(text);
  const last = lines.length - 1;
  if (lines.length > 0 && lines.every((l) => BULLET.test(l))) {
    return (
      <ul className="bullets">
        {lines.map((l, i) => (
          <li key={i}>
            <Line text={l.replace(BULLET, "")} streaming={streaming && i === last} />
            {streaming && i === last && <Caret />}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="prose">
      {lines.map((l, i) => (
        <p key={i}>
          <Line text={l.replace(BULLET, "• ")} streaming={streaming && i === last} />
          {streaming && i === last && <Caret />}
        </p>
      ))}
    </div>
  );
}
