// Coding mode's answers (ARCHITECTURE.md, "Coding mode"): the gateway's Markdown subset, drawn
// for reading at a glance and copying exactly. "### 1. Title" headings split the tasks of one
// screen, **bold** labels lead each part (Answer, Approach, Code, Complexity), `names` are
// inline code, and fenced blocks are code: monospace, highlighted, each with its own Copy,
// which copies the code exactly as written (blank lines and indentation kept). A block still
// streaming shows as it grows.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import kotlin from "highlight.js/lib/languages/kotlin";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import { COPIED_MS, copyText } from "./follow";

const LANGUAGES = { bash, c, cpp, csharp, go, java, javascript, kotlin, python, ruby, rust, sql, swift, typescript };
for (const [name, lang] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, lang);
const ALIASES: Record<string, string> = {
  py: "python", python3: "python", js: "javascript", node: "javascript", jsx: "javascript", ts: "typescript",
  tsx: "typescript", "c++": "cpp", cc: "cpp", cs: "csharp", "c#": "csharp", golang: "go", kt: "kotlin",
  rb: "ruby", rs: "rust", sh: "bash", shell: "bash", postgresql: "sql", postgres: "sql", mysql: "sql",
  sqlite: "sql", tsql: "sql",
};

/** The highlight.js language for a fence's tag, or null to show the code plain. */
export function languageOf(tag: string): string | null {
  const t = tag.trim().toLowerCase();
  const name = ALIASES[t] ?? t;
  return name in LANGUAGES ? name : null;
}

export type Block = { kind: "text"; lines: string[] } | { kind: "code"; lang: string; code: string; open: boolean };

/** Text and fenced code, in order. An unclosed fence (still streaming) is an open code block. */
export function splitBlocks(text: string): Block[] {
  const out: Block[] = [];
  let text_: string[] = [];
  let code: { lang: string; lines: string[] } | null = null;
  for (const line of text.split("\n")) {
    const fence = /^\s*```\s*([\w+#.-]*)/.exec(line);
    if (code === null) {
      if (fence) {
        if (text_.length) out.push({ kind: "text", lines: text_ });
        text_ = [];
        code = { lang: fence[1] ?? "", lines: [] };
      } else {
        text_.push(line);
      }
    } else if (/^\s*```\s*$/.test(line)) {
      out.push({ kind: "code", lang: code.lang, code: code.lines.join("\n"), open: false });
      code = null;
    } else {
      code.lines.push(line);
    }
  }
  if (code !== null) out.push({ kind: "code", lang: code.lang, code: code.lines.join("\n"), open: true });
  else if (text_.length) out.push({ kind: "text", lines: text_ });
  return out;
}

/** **bold** and `code` inside a line, as React nodes (no HTML is ever injected). */
export function inline(line: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push(line.slice(last, m.index));
    out.push(m[1] !== undefined ? <strong key={m.index}>{m[1]}</strong> : <code key={m.index}>{m[2]}</code>);
    last = m.index + m[0].length;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

function CodeBlock({ lang, code, open }: { lang: string; code: string; open: boolean }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), COPIED_MS);
    return () => clearTimeout(t);
  }, [copied]);
  const language = languageOf(lang);
  const html = useMemo(
    () => (language ? hljs.highlight(code, { language, ignoreIllegals: true }).value : null),
    [code, language],
  );
  return (
    <figure className={`code-block${open ? " is-open" : ""}`}>
      <figcaption>
        <span className="code-lang">{lang || "code"}</span>
        <button type="button" className="copy" disabled={open || !code} onClick={() => void copyText(code).then(setCopied)} aria-label={lang ? `Copy the ${lang} code` : "Copy the code"}>
          {copied ? "Copied" : "Copy code"}
        </button>
      </figcaption>
      <pre>
        {html !== null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : <code>{code}</code>}
      </pre>
    </figure>
  );
}

const Caret = () => <span className="ov-caret" aria-hidden="true" />;

/** A coding answer, streaming or done. */
export function CodeAnswer({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const blocks = splitBlocks(text);
  const last = blocks.length - 1;
  return (
    <div className="code-answer">
      {blocks.map((b, i) =>
        b.kind === "code" ? (
          <CodeBlock key={i} lang={b.lang} code={b.code} open={b.open && streaming} />
        ) : (
          <div key={i} className="code-text">
            {b.lines
              .filter((l) => l.trim())
              .map((l, j, all) => {
                const heading = /^#{1,4}\s+(.*)$/.exec(l);
                const tail = streaming && i === last && j === all.length - 1 && <Caret />;
                return heading ? (
                  <h3 key={j} className="code-task">
                    {inline(heading[1] ?? "")}
                    {tail}
                  </h3>
                ) : (
                  <p key={j}>
                    {inline(l.replace(/^\s*[-*]\s+/, "• "))}
                    {tail}
                  </p>
                );
              })}
          </div>
        ),
      )}
    </div>
  );
}
