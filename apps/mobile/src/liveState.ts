import {
  DEVELOPMENT_BUILD,
  HTTP_TEST_BUILD,
  transportURL,
} from "./transport.ts";

export type Event = {
  type: string;
  seq?: number;
  question_id?: string;
  text?: string;
  pass?: string;
  [key: string]: unknown;
};
export type Guidance = {
  id: string;
  question: string;
  fast: string;
  deep: string;
  done: boolean;
};
export type LiveState = {
  seq: number;
  items: Guidance[];
  connected: boolean;
  ended: boolean;
  status: string;
  memory: string;
  spokenLanguage: string | null;
  languageSwitching: boolean;
};
export const emptyLive = (): LiveState => ({
  seq: 0,
  items: [],
  connected: false,
  ended: false,
  status: "Connecting",
  memory: "",
  spokenLanguage: null,
  languageSwitching: false,
});
// Keep the request identity until this authoritative event arrives. A relay command
// receipt can arrive before or after it and does not confirm the active language.
export function languageAcknowledgement(
  message: Event,
  requestId: string | null,
): { error: string | null } | null {
  if (
    !requestId ||
    message.type !== "session.language" ||
    message.request_id !== requestId
  )
    return null;
  return { error: typeof message.error === "string" ? message.error : null };
}
export function event(state: LiveState, message: Event): LiveState {
  if (
    !message ||
    typeof message !== "object" ||
    typeof message.type !== "string"
  )
    throw new Error("Invalid session event");
  if (
    message.seq !== undefined &&
    (!Number.isSafeInteger(message.seq) || message.seq < 0)
  )
    throw new Error("Invalid event sequence");
  if (message.type === "companion.snapshot") {
    if (!Array.isArray(message.messages) || message.messages.length > 20000)
      throw new Error("Invalid session snapshot");
    const messages = message.messages as Event[];
    for (const item of messages) {
      if (
        !item ||
        typeof item !== "object" ||
        item.type === "companion.snapshot" ||
        (item.seq !== undefined &&
          (!Number.isSafeInteger(item.seq) || item.seq < 0))
      )
        throw new Error("Invalid snapshot event");
    }
    let next = emptyLive();
    // Server folds preserve insertion order while merged deltas acquire newer seq values.
    // Restore the complete folded state in sequence order before applying live deduplication.
    for (const item of [...messages].sort(
      (a, b) => (a.seq ?? 0) - (b.seq ?? 0),
    ))
      next = event(next, item);
    return {
      ...next,
      connected: state.connected,
      languageSwitching: state.languageSwitching,
      spokenLanguage: next.spokenLanguage ?? state.spokenLanguage,
      seq: message.seq ?? next.seq,
      status: "Connected",
    };
  }
  if (message.type === "companion.host")
    return {
      ...state,
      connected: message.connected === true,
      status: message.connected
        ? "Desktop audio connected"
        : "Desktop disconnected — phone microphone is off",
    };
  if (message.type === "companion.ended")
    return { ...state, ended: true, connected: false, status: "Call ended" };
  if (typeof message.seq === "number" && message.seq <= state.seq) return state;
  const next = {
    ...state,
    seq: message.seq ?? state.seq,
    items: state.items.map((x) => ({ ...x })),
  };
  if (
    message.type === "session.language" ||
    ((message.type === "session.ready" || message.type === "companion.ready") &&
      message.spoken_language !== undefined)
  ) {
    if (
      typeof message.spoken_language !== "string" ||
      !/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(message.spoken_language)
    )
      throw new Error("Invalid session language");
    next.spokenLanguage = message.spoken_language;
  }
  if (
    message.type === "session.language" &&
    ((message.error !== undefined && typeof message.error !== "string") ||
      (message.request_id !== undefined &&
        typeof message.request_id !== "string"))
  )
    throw new Error("Invalid language change response");
  if (message.type === "session.ready" || message.type === "companion.ready") {
    if (
      message.language_switching !== undefined &&
      typeof message.language_switching !== "boolean"
    )
      throw new Error("Invalid language switching capability");
    next.languageSwitching = message.language_switching === true;
  }
  if (message.type === "status" && message.service === "memory")
    next.memory = String(message.message ?? "");
  const id = message.question_id;
  if (!id) return next;
  if (
    typeof id !== "string" ||
    (message.text !== undefined && typeof message.text !== "string")
  )
    throw new Error("Invalid question event");
  let question = next.items.find((x) => x.id === id);
  if (message.type === "question.cancelled")
    return { ...next, items: next.items.filter((x) => x.id !== id) };
  if (
    !question &&
    ["question.detected", "answer.delta"].includes(message.type)
  ) {
    question = { id, question: "Question", fast: "", deep: "", done: false };
    next.items.push(question);
    next.items = next.items.slice(-100);
  }
  if (!question) return next;
  if (message.type === "question.detected")
    question.question = message.text ?? "";
  if (message.type === "answer.delta") {
    if (message.pass === "deep") question.deep += message.text ?? "";
    else question.fast += message.text ?? "";
  }
  if (message.type === "answer.done") question.done = true;
  if (message.type === "answer.error") {
    question.done = true;
    if (!question.fast && !question.deep)
      question.fast =
        "Guidance unavailable. Ask again when the connection is ready.";
  }
  return next;
}
export function command(
  id: string,
  kind: "answer" | "followup" | "end" | "set_language",
  text = "",
  confirmed = false,
  summarize = true,
) {
  if (kind === "end" && !confirmed)
    throw new Error("Confirm ending the desktop call first.");
  if (kind === "followup" && text.trim().length > 4000)
    throw new Error("Keep follow-ups under 4000 characters.");
  if (kind === "followup" && !text.trim())
    throw new Error("Enter a follow-up.");
  if (
    kind === "set_language" &&
    !/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(text)
  )
    throw new Error("Choose a supported spoken language.");
  return {
    type: "companion.command",
    command_id: id,
    kind,
    ...(kind === "followup" ? { text: text.trim() } : {}),
    ...(kind === "end" ? { confirmed: true, summarize } : {}),
    ...(kind === "set_language" ? { spoken_language: text } : {}),
  };
}
export function gatewayURL(
  raw: string,
  development = DEVELOPMENT_BUILD,
  httpTest = HTTP_TEST_BUILD,
): string {
  return transportURL(raw, "websocket", development, httpTest).toString();
}
