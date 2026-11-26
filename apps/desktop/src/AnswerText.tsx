// Streamed answer text: bullet lines as a list, citation ids and legacy suggestion
// notation omitted. While a pass is still writing, a caret follows the last word;
// a bullet appears only with its first word.
const CITE = /[ \t]*\[n:[0-9a-z]+\]/gi;
/** The end of a streaming line that may be a citation still being written: "[", "[n", "[n:3f". */
const CITE_PARTIAL = /[ \t]*\[(n(:[0-9a-z]*)?)?$/i;
const SUGGESTED = /[ \t]*\[~\]/g;
const SUGGESTED_PARTIAL = /[ \t]*\[~$/;
const BULLET = /^\s*[-*•]\s+/;
/** A line that is only a bullet's marker so far ("-" or "- "), still waiting for its words. */
const MARKER_ONLY = /^\s*[-*•]\s*$/;

/** Omit legacy suggestion notation from a prose token, without adding styling. */
export function withoutSuggestionMarkers(text: string, streaming = false): string {
  const out = text.replace(SUGGESTED, "");
  return streaming ? out.replace(SUGGESTED_PARTIAL, "") : out;
}

/** A readable line, without citations or legacy suggestion notation. */
export function readableLine(text: string, streaming = false): string {
  const out = text.replace(CITE, "");
  return withoutSuggestionMarkers(streaming ? out.replace(CITE_PARTIAL, "") : out, streaming);
}

/** The lines worth drawing: no empty notation-only row or bullet without words. */
export function answerLines(text: string, streaming = false): string[] {
  const lines = text.split("\n").filter((line) => line.trim() && !MARKER_ONLY.test(line));
  return lines
    .map((line, i) => readableLine(line, streaming && i === lines.length - 1))
    .filter((line) => line.trim() && !MARKER_ONLY.test(line));
}

const Caret = () => <span className="ov-caret" aria-hidden="true" />;

export function AnswerText({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const lines = answerLines(text, streaming);
  const last = lines.length - 1;
  if (lines.length > 0 && lines.every((l) => BULLET.test(l))) {
    return (
      <ul className="bullets">
        {lines.map((l, i) => (
          <li key={i}>
            <span>{l.replace(BULLET, "")}</span>
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
          <span>{l.replace(BULLET, "• ")}</span>
          {streaming && i === last && <Caret />}
        </p>
      ))}
    </div>
  );
}
