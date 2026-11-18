// Numbers the lines of each .code block and colours keywords, strings and comments, as an
// editor would. Runs before the headless screenshot.
const TOKENS =
  /("[^"]*"|'[^']*')|\b(def|class|return|for|while|if|elif|else|in|not|and|or|import|from|self|None|True|False|function|const|let|var|new|public|private|static|int|void|string|List|SELECT|FROM|WHERE|GROUP|BY|HAVING|ORDER|JOIN|ON|AS)\b/g;
const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
for (const block of document.querySelectorAll(".code")) {
  const lines = block.textContent.replace(/^\n/, "").replace(/\s+$/, "").split("\n");
  block.innerHTML = lines
    .map((line) => {
      if (/^\s*(#|\/\/|--)/.test(line)) return `<span class="l"><span class="cm">${escape(line)}</span></span>`;
      let html = "";
      let last = 0;
      for (const m of line.matchAll(TOKENS)) {
        html += escape(line.slice(last, m.index));
        html += `<span class="${m[1] ? "st" : "kw"}">${escape(m[0])}</span>`;
        last = m.index + m[0].length;
      }
      html += escape(line.slice(last));
      return `<span class="l">${html || " "}</span>`;
    })
    .join("");
}
