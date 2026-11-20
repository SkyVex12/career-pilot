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
};
export const emptyLive = (): LiveState => ({
  seq: 0,
  items: [],
  connected: false,
  ended: false,
  status: "Connecting",
  memory: "",
});
export function event(state: LiveState, message: Event): LiveState {
  if (message.type === "companion.snapshot") {
    let next = emptyLive();
    for (const item of (message.messages ?? []) as Event[])
      next = event(next, item);
    return {
      ...next,
      connected: state.connected,
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
  if (message.type === "status" && message.service === "memory")
    next.memory = String(message.message ?? "");
  const id = message.question_id;
  if (!id) return next;
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
  return next;
}
export function command(
  id: string,
  kind: "answer" | "followup" | "end",
  text = "",
  confirmed = false,
  summarize = true,
) {
  if (kind === "end" && !confirmed)
    throw new Error("Confirm ending the desktop call first.");
  if (kind === "followup" && !text.trim())
    throw new Error("Enter a follow-up.");
  return {
    type: "companion.command",
    command_id: id,
    kind,
    ...(kind === "followup" ? { text: text.trim() } : {}),
    ...(kind === "end" ? { confirmed: true, summarize } : {}),
  };
}
export function gatewayURL(
  raw: string,
  development = DEVELOPMENT_BUILD,
  httpTest = HTTP_TEST_BUILD,
): string {
  return transportURL(raw, "websocket", development, httpTest).toString();
}
