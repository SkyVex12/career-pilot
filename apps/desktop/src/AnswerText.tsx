// Streamed answer text: bullet lines as a list, citations like [n:3f2a] as small tags.
const CITE = /\[(n:[0-9a-z]+)\]/gi;

function Line({ text }: { text: string }) {
  const parts = text.split(CITE);
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <sup key={i} className="cite">
            {p}
          </sup>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
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
