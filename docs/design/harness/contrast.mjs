// WCAG contrast of today's colours and the proposal's tokens (docs/design/ui-audit.md and
// ui-proposal.md quote these numbers). Run: node docs/design/harness/contrast.mjs
// The overlay rows composite the panel and text at the given opacity over a light backdrop.
const hex = (h) => { h = h.replace("#", ""); if (h.length === 3) h = [...h].map((c) => c + c).join(""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const L = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const cr = (a, b) => { const [x, y] = [L(a), L(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const mix = (a, b, t) => a.map((v, i) => Math.round(v * t + b[i] * (1 - t)));
const row = (label, fg, bg) => console.log(label.padEnd(52), cr(hex(fg), hex(bg)).toFixed(2));
console.log("== today");
row("muted #667085 on panel #fff", "#667085", "#fff");
row("chip ok #16a34a on ~#f3faf5", "#16a34a", "#f3faf5");
row("chip busy #d97706 on ~#fdf8f0", "#d97706", "#fdf8f0");
row("chip bad #dc2626 on #fef3f2", "#dc2626", "#fef3f2");
row("accent link #2563eb on #fff", "#2563eb", "#fff");
row("white on accent #2563eb", "#fff", "#2563eb");
row("focus ring (22% blue on white) vs white", mix(hex("#2563eb"), hex("#fff"), 0.22).map(v=>v.toString(16).padStart(2,"0")).join(""), "#fff");
row("dark muted #8b949e on #1b1f24", "#8b949e", "#1b1f24");
row("overlay muted #9aa3ae on #191c21", "#9aa3ae", "#191c21");
// overlay over a light backdrop at reduced opacity: the whole window fades
const back = hex("#e8e2d6"), panel = mix(hex("#14171c"), back, 0.94), text = hex("#eef1f5");
for (const o of [1, 0.6, 0.35]) {
  const bgPx = mix(panel, back, o), txPx = mix(text, back, o);
  console.log(`overlay text at opacity ${o} over light content`.padEnd(52), cr(txPx, bgPx).toFixed(2));
}
console.log("== proposed light");
for (const [n, f, b] of [["text #14171c / surface","#14171c","#fff"],["text-2 #4a5361 / surface","#4a5361","#fff"],["text-3 #626b79 / surface","#626b79","#fff"],["text-3 / surface-2 #f1f3f6","#626b79","#f1f3f6"],["text-3 / bg #f5f6f8","#626b79","#f5f6f8"],["accent #2457d6 / surface","#2457d6","#fff"],["on-accent #fff / accent","#fff","#2457d6"],["success #14753a / success-soft #e8f5ec","#14753a","#e8f5ec"],["warning #8a5300 / warning-soft #fdf3e1","#8a5300","#fdf3e1"],["danger #b42318 / danger-soft #fdecea","#b42318","#fdecea"],["white / danger #b42318","#fff","#b42318"],["focus #2457d6 / surface (non-text 3:1)","#2457d6","#fff"],["border-strong #858e9b / surface (inputs 3:1)","#858e9b","#fff"]]) row(n,f,b);
console.log("== proposed dark");
for (const [n, f, b] of [["text #e8ebf0 / surface #171a20","#e8ebf0","#171a20"],["text-2 #b3bbc7 / surface","#b3bbc7","#171a20"],["text-3 #8d96a4 / surface","#8d96a4","#171a20"],["text-3 / surface-2 #20242c","#8d96a4","#20242c"],["accent #7aa5ff / surface","#7aa5ff","#171a20"],["on-accent #0b1020 / accent-solid #7aa5ff","#0b1020","#7aa5ff"],["success #5bd08a / surface","#5bd08a","#171a20"],["warning #f2b84b / surface","#f2b84b","#171a20"],["danger #ff8a80 / surface","#ff8a80","#171a20"],["focus #7aa5ff / bg #0f1115","#7aa5ff","#0f1115"],["border-strong #646c7a / surface (3:1)","#646c7a","#171a20"]]) row(n,f,b);
console.log("== proposed overlay (text floor 0.92 over light content, panel at 35%)");
const ovText = hex("#f4f6f9"), ovPanel = hex("#101216");
for (const o of [1, 0.6, 0.35]) {
  const bgPx = mix(ovPanel, back, Math.max(o, 0.62)); // panel never below 55% behind text: a scrim under the text block
  const txPx = mix(ovText, bgPx, 0.92 + 0.08 * o);
  console.log(`fast answer at opacity ${o}`.padEnd(52), cr(txPx, bgPx).toFixed(2));
}

console.log("== stage 4 overlay as built (panel 0.94*alpha, card backing max(0.62, alpha), text opaque)");
for (const backHex of ["#e8e2d6", "#ffffff", "#000000"]) {
  for (const a of [1, 0.6, 0.35, 0.3]) {
    const panelPx = mix(hex("#101216"), hex(backHex), 0.94 * a);
    const cardPx = mix(hex("#101216"), panelPx, Math.max(0.62, a));
    console.log(`fast answer #f4f6f9, opacity ${a}, over ${backHex}`.padEnd(52), cr(hex("#f4f6f9"), cardPx).toFixed(2));
  }
}
