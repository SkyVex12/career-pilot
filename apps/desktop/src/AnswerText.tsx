// Streamed answer text: bullet lines as a list, citations like [n:3f2a] as small tags, and
// suggested details (a line ending in [~]: probably true of the user's work, not in their data)
// dimmed with a "check" tag, so the user sees what to verify before saying it.
const CITE = /\[(n:[0-9a-z]+)\]/gi;
const SUGGESTED = /\s*\[~\]/g;

export function isSuggested(text: string): boolean {
  return /\[~\]/.test(text);
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

export function AnswerText({ text }: { text: string }) {
  const lines = text.split("\n").filter((l) => l.trim());
  const bullet = /^\s*[-*•]\s+/;
  if (lines.length > 0 && lines.every((l) => bullet.test(l))) {
    return (
      <ul className="bullets">
        {lines.map((l, i) => (
          <li key={i}>
            <Line text={l.replace(bullet, "")} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="prose">
      {lines.map((l, i) => (
        <p key={i}>
          <Line text={l.replace(bullet, "• ")} />
        </p>
      ))}
    </div>
  );
}
