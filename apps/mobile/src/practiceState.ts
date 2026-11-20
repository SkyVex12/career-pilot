/** Bounded wire parsing and request identity for the phone practice experience. */
export type PracticeFeedback = {
  rubric_version: string;
  dimensions: Record<string, number>;
  strengths: string[];
  improvements: string[];
  excerpts: string[];
  uncertainty: string[];
  suggested_wording: string;
};
export type PracticeComparison = {
  note: string;
  comparable: boolean;
  summary: string;
  improvements: string[];
  remaining_gaps: string[];
  recurring_gaps: string[];
  dimensions: Record<string, { previous: number; current: number }>;
  evidence: { previous: string[]; current: string[] };
};
export type PracticeQuestion = {
  id: string;
  text: string;
  kind: string;
  hint: string;
  sample: string;
  saved_for_later: boolean;
  attempts: {
    id: string;
    answer: string;
    assistance: string[];
    feedback: PracticeFeedback | null;
    comparison: PracticeComparison | null;
  }[];
};
export type PracticeSession = {
  id: string;
  status: string;
  setup: { topic: string; duration_minutes: number };
  questions: PracticeQuestion[];
  current_question_id: string | null;
  spoken_language: string;
  elapsed_seconds: number;
  error: string;
  voice_available: boolean;
  speech_available: boolean;
  retention_notice: string;
};
export type PracticeEntry = Pick<PracticeSession, "id" | "status" | "setup">;
const statuses = new Set([
  "preparing",
  "processing",
  "answering",
  "paused",
  "finished",
  "failed",
]);
const incompatible = () =>
  new Error(
    "The server returned an incompatible practice response. Update the server, then try again.",
  );
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw incompatible();
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 16000): string {
  return typeof value === "string" ? value.slice(0, limit) : "";
}
function required(value: unknown, limit = 500): string {
  const result = text(value, limit);
  if (!result.trim()) throw incompatible();
  return result;
}
function list(value: unknown, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw incompatible();
  return value;
}
function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .slice(0, 30)
        .filter((v): v is string => typeof v === "string")
        .map((v) => v.slice(0, 2000))
    : [];
}
function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
function dimensions(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .slice(0, 20)
      .flatMap(([key, raw]) => {
        const score =
          typeof raw === "number"
            ? raw
            : raw && typeof raw === "object"
              ? (raw as { score?: unknown }).score
              : undefined;
        return typeof score === "number" &&
          Number.isFinite(score) &&
          score >= 0 &&
          score <= 4
          ? [[key.slice(0, 100), score]]
          : [];
      }),
  );
}
function feedback(value: unknown): PracticeFeedback | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = object(value);
  return {
    rubric_version: text(row.rubric_version, 100),
    dimensions: dimensions(row.dimensions),
    strengths: strings(row.strengths),
    improvements: strings(row.improvements),
    excerpts: strings(row.excerpts),
    uncertainty: strings(row.uncertainty),
    suggested_wording: text(row.suggested_wording),
  };
}
function comparison(value: unknown): PracticeComparison | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = object(value),
    rawDimensions = row.dimensions,
    rawEvidence = row.evidence;
  const scores: PracticeComparison["dimensions"] = {};
  if (
    rawDimensions &&
    typeof rawDimensions === "object" &&
    !Array.isArray(rawDimensions)
  )
    for (const [key, value] of Object.entries(rawDimensions).slice(0, 20)) {
      if (!value || typeof value !== "object") continue;
      const pair = value as Record<string, unknown>;
      if (
        typeof pair.previous === "number" &&
        typeof pair.current === "number" &&
        Number.isFinite(pair.previous) &&
        Number.isFinite(pair.current)
      )
        scores[key.slice(0, 100)] = {
          previous: pair.previous,
          current: pair.current,
        };
    }
  const evidence =
    rawEvidence &&
    typeof rawEvidence === "object" &&
    !Array.isArray(rawEvidence)
      ? (rawEvidence as Record<string, unknown>)
      : {};
  return {
    note: text(row.note),
    comparable: row.comparable === true,
    summary: text(row.summary),
    improvements: strings(row.improvements),
    remaining_gaps: strings(row.remaining_gaps),
    recurring_gaps: strings(row.recurring_gaps),
    dimensions: scores,
    evidence: {
      previous: strings(evidence.previous),
      current: strings(evidence.current),
    },
  };
}
function entry(value: unknown): PracticeEntry {
  const row = object(value),
    setup = object(row.setup),
    status = required(row.status, 30);
  if (!statuses.has(status)) throw incompatible();
  return {
    id: required(row.id, 100),
    status,
    setup: {
      topic: required(setup.topic),
      duration_minutes: Math.max(
        3,
        Math.min(60, number(setup.duration_minutes, 15)),
      ),
    },
  };
}
export function parsePracticeSession(value: unknown): PracticeSession {
  const row = object(value),
    base = entry(value);
  const questions = list(row.questions, 200).map((value) => {
    const q = object(value);
    return {
      id: required(q.id, 100),
      text: required(q.text, 8000),
      kind: text(q.kind, 100),
      hint: text(q.hint),
      sample: text(q.sample),
      saved_for_later: q.saved_for_later === true,
      attempts: list(q.attempts ?? [], 30).map((value) => {
        const a = object(value);
        return {
          id: required(a.id, 100),
          answer: text(a.answer),
          assistance: strings(a.assistance),
          feedback: feedback(a.feedback),
          comparison: comparison(a.comparison),
        };
      }),
    };
  });
  if (new Set(questions.map((q) => q.id)).size !== questions.length)
    throw incompatible();
  const current = text(row.current_question_id, 100) || null;
  if (
    base.status === "answering" &&
    (!current || !questions.some((q) => q.id === current))
  )
    throw incompatible();
  return {
    ...base,
    questions,
    current_question_id: current,
    spoken_language: text(row.spoken_language, 100),
    elapsed_seconds: Math.max(0, number(row.elapsed_seconds)),
    error: text(row.error),
    voice_available: row.voice_available === true,
    speech_available: row.speech_available === true,
    retention_notice: text(row.retention_notice),
  };
}
export function parsePracticeHistory(value: unknown): PracticeEntry[] {
  return list(object(value).sessions, 200).map(entry);
}
export function parsePracticeChoices(
  value: unknown,
  kind: "languages" | "applications",
): { value: string; label: string }[] {
  const row = object(value);
  return list(kind === "languages" ? row.items : row.options, 1000).map(
    (value) => {
      const item = object(value);
      return {
        value: required(item.id, 100),
        label: required(kind === "languages" ? item.name : item.label),
      };
    },
  );
}
/** Load each setup resource independently so a failed endpoint cannot hide the other. */
export async function loadPracticeSetup(
  request: (path: string) => Promise<unknown>,
) {
  const results = await Promise.allSettled([
    request("/call-languages").then((value) =>
      parsePracticeChoices(value, "languages"),
    ),
    request("/ask/context-options?kind=application").then((value) =>
      parsePracticeChoices(value, "applications"),
    ),
  ]);
  return {
    languages: results[0].status === "fulfilled" ? results[0].value : null,
    applications: results[1].status === "fulfilled" ? results[1].value : null,
    unavailable: results.flatMap((result, index) =>
      result.status === "rejected"
        ? [index === 0 ? "spoken languages" : "application context"]
        : [],
    ),
  };
}
export function parsePracticeContext(
  value: unknown,
): { label: string; excerpt: string }[] {
  return list(object(value).sources, 30).map((value) => {
    const item = object(value);
    return { label: required(item.label), excerpt: text(item.excerpt, 4000) };
  });
}
export function parsePracticeTranscript(value: unknown): {
  text: string;
  silence: boolean;
} {
  const row = object(value);
  if (
    typeof row.text !== "string" ||
    row.text.length > 16000 ||
    typeof row.silence !== "boolean"
  )
    throw incompatible();
  return { text: row.text, silence: row.silence };
}
export function parsePracticeSpeech(value: unknown): string {
  const encoded = object(value).audio_base64;
  if (
    typeof encoded !== "string" ||
    !encoded.length ||
    encoded.length > 12 * 1024 * 1024 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
  )
    throw incompatible();
  return encoded;
}
export type PracticeActionInput = {
  sessionId: string;
  action: string;
  question_id?: string;
  answer?: string;
};
export type PracticeOperation = {
  input: PracticeActionInput;
  body: {
    request_id: string;
    action: string;
    question_id?: string;
    answer?: string;
  };
};
export function practiceOperation(
  previous: PracticeOperation | null,
  input: PracticeActionInput,
  id: () => string,
): PracticeOperation {
  if (previous && JSON.stringify(previous.input) === JSON.stringify(input))
    return previous;
  const { sessionId: _, ...body } = input;
  return { input, body: { ...body, request_id: id() } };
}
export type PracticeTicket = {
  revision: number;
  controller: AbortController;
  signal: AbortSignal;
};
/** Session/question changes abort pending work even if a native operation settles late. */
export class PracticeScope {
  private revision = 0;
  private tickets = new Set<PracticeTicket>();
  begin(): PracticeTicket {
    const controller = new AbortController(),
      ticket = {
        revision: this.revision,
        controller,
        signal: controller.signal,
      };
    this.tickets.add(ticket);
    return ticket;
  }
  current(ticket: PracticeTicket): boolean {
    return ticket.revision === this.revision && !ticket.signal.aborted;
  }
  finish(ticket: PracticeTicket): void {
    this.tickets.delete(ticket);
  }
  invalidate(): void {
    this.revision++;
    for (const ticket of this.tickets) ticket.controller.abort();
    this.tickets.clear();
  }
}
/** Calling disposed Expo objects may throw synchronously; every cleanup must still run. */
export async function cleanupPracticeMedia(steps: {
  pause: () => unknown;
  stop: () => unknown;
  discard: () => unknown;
  release: () => unknown;
}): Promise<void> {
  for (const step of [steps.pause, steps.stop, steps.discard, steps.release]) {
    try {
      await step();
    } catch {
      /* Best-effort cleanup must never reject after unmount. */
    }
  }
}
/** Poll after the previous request settles, never on an overlapping interval. */
export function pollPractice(
  work: () => Promise<void>,
  delay = 1500,
): () => void {
  let stopped = false,
    timer: ReturnType<typeof setTimeout> | undefined;
  const next = async () => {
    try {
      await work();
    } catch {
      /* Caller reports actionable failure. */
    } finally {
      if (!stopped) timer = setTimeout(next, delay);
    }
  };
  timer = setTimeout(next, delay);
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

/** A stale async finally must not release a newer operation's UI lock. */
export class PracticeBusy {
  private next = 0;
  private owner: number | null = null;
  begin(): number | null {
    if (this.owner !== null) return null;
    this.owner = ++this.next;
    return this.owner;
  }
  finish(ticket: number): boolean {
    if (this.owner !== ticket) return false;
    this.owner = null;
    return true;
  }
  invalidate(): void {
    this.owner = null;
  }
  get active(): boolean {
    return this.owner !== null;
  }
}

/** Serialize native setup/teardown so a late prepare cannot outlive interruption cleanup. */
export class PracticeMediaQueue {
  private tail: Promise<unknown> = Promise.resolve();
  run<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.tail.then(operation, operation);
    this.tail = result.catch(() => {});
    return result;
  }
}
