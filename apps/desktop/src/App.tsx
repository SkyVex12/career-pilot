import type { Mode } from "@copilot/protocol";

// Placeholder shell. Audio capture, the gateway connection and the overlay come in the
// desktop build step (ARCHITECTURE.md, "Desktop client").
const MODES: Mode[] = ["interview", "standup", "meeting"];

export default function App() {
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: 24 }}>
      <h1>Copilot</h1>
      <p>Desktop shell. Modes: {MODES.join(", ")}.</p>
    </main>
  );
}
