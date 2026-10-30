// Streamed answer text: bullet lines as a list, citations like [n:3f2a] as small tags, and
// suggested details (a line ending in [~]: probably true of the user's work, not in their data)
// dimmed with a "check" tag, so the user sees what to verify before saying it. While a pass is
// still writing, a caret follows the last word; a bullet appears only with its first word.
const CITE = /\[(n:[0-9a-z]+)\]/gi;
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

function Line({ text }: { text: string }) {
  const suggested = isSuggested(text);
  const parts = text.replace(SUGGESTED, "").split(CITE);
  return (
    <span className={suggested ? "suggested" : undefined}>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <sup key={i} className="cite">
            {p}
          </sup>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
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
            <Line text={l.replace(BULLET, "")} />
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
          <Line text={l.replace(BULLET, "• ")} />
          {streaming && i === last && <Caret />}
        </p>
      ))}
    </div>
  );
}
