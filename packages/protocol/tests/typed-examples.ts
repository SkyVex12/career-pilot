// Compile-time contract: the examples from ARCHITECTURE.md section 1 must satisfy the
// generated types. Checked by `pnpm typecheck`; nothing runs at test time.
import type { AnswerDelta, SessionStart } from "../src/index.ts";

export const sessionStart = {
  type: "session.start",
  mode: "interview",
  job_id: "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b33",
  platform: "zoom",
  client_version: "1.4.0",
  consent: { store_transcript: true },
  audio: { sample_rate: 16000, encoding: "pcm16" },
} satisfies SessionStart;

export const answerDelta = {
  type: "answer.delta",
  seq: 412,
  question_id: "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210",
  pass: "fast",
  text: "- Led the payments migration at Acme, cut p95 latency 40% [n:3f2a]\n",
} satisfies AnswerDelta;
