import { emptyLive, event, type Event, type LiveState } from "./liveState.ts";
import { gatewayURL } from "./liveState.ts";
import { withDeadline } from "./requestScope.ts";

export type AssistantSeat = {
  seat_id: string;
  session_id: string;
  host_name: string;
  mode: string;
  title: string | null;
  status: "invited" | "accepted" | "admitted";
  capabilities: string[];
  expires_at: string | null;
};
export type TranscriptLine = { id: string; text: string; speaker: string };
export type Note = { id: string; text: string; result: string };
export type AssistantState = {
  live: LiveState;
  transcript: TranscriptLine[];
  capabilities: string[];
  sharing: boolean;
  hostConnected: boolean;
  connected: boolean;
  status: string;
  terminal: boolean;
  notes: Note[];
};
export const emptyAssistant = (): AssistantState => ({
  live: emptyLive(),
  transcript: [],
  capabilities: [],
  sharing: false,
  hostConnected: false,
  connected: false,
  status: "Connecting…",
  terminal: false,
  notes: [],
});
const forwarded = new Set([
  "transcript.partial",
  "transcript.final",
  "question.detected",
  "question.cancelled",
  "answer.delta",
  "answer.done",
  "answer.error",
  "status",
  "session.language",
]);
const caps = (value: unknown): string[] => {
  if (!Array.isArray(value) || value.some((x) => typeof x !== "string"))
    throw new Error("Invalid assistant permissions.");
  return value;
};
export function parseSeats(value: unknown): AssistantSeat[] {
  const seats = (value as { seats?: unknown })?.seats;
  if (
    !Array.isArray(seats) ||
    seats.some(
      (s) =>
        !s ||
        typeof s.seat_id !== "string" ||
        typeof s.session_id !== "string" ||
        typeof s.host_name !== "string" ||
        typeof s.mode !== "string" ||
        (s.title != null && typeof s.title !== "string") ||
        (s.expires_at != null && typeof s.expires_at !== "string") ||
        !["invited", "accepted", "admitted"].includes(s.status) ||
        !Array.isArray(s.capabilities),
    )
  )
    throw new Error("The server returned unsupported call invitations.");
  return seats.map((s) => ({ ...s, capabilities: caps(s.capabilities) }));
}
export function assistantEvent(
  state: AssistantState,
  message: Event,
): AssistantState {
  if (!message || typeof message.type !== "string")
    throw new Error("Invalid assistant event.");
  if (message.type === "seat.ready")
    return {
      ...state,
      connected: true,
      terminal: false,
      capabilities: caps(message.capabilities),
      sharing: message.sharing === true,
      hostConnected: message.host_connected === true,
      status: "Connected",
    };
  if (message.type === "seat.host")
    return {
      ...state,
      sharing: message.sharing === true,
      hostConnected: message.connected === true,
    };
  if (message.type === "seat.update")
    return { ...state, capabilities: caps(message.capabilities) };
  if (message.type === "seat.command.result")
    return {
      ...state,
      notes: state.notes.map((n) =>
        n.id === message.command_id
          ? { ...n, result: String(message.result) }
          : n,
      ),
    };
  if (message.type === "seat.snapshot") {
    if (
      !Array.isArray(message.messages) ||
      message.messages.length > 20000 ||
      message.messages.some(
        (m) => !m || typeof m !== "object" || !forwarded.has((m as Event).type),
      )
    )
      throw new Error("Invalid assistant snapshot.");
    // Folded deltas have the most recent seq, so reconstruct in sequence order.
    let fresh = {
      ...state,
      live: emptyLive(),
      transcript: [] as TranscriptLine[],
    };
    for (const item of [...(message.messages as Event[])].sort(
      (a, b) => (a.seq ?? 0) - (b.seq ?? 0),
    ))
      fresh = assistantEvent(fresh, item);
    if (!Number.isSafeInteger(message.seq) || (message.seq ?? -1) < 0)
      throw new Error("Invalid assistant snapshot position.");
    return { ...fresh, live: { ...fresh.live, seq: message.seq! } };
  }
  if (!forwarded.has(message.type)) return state;
  if (!Number.isSafeInteger(message.seq) || (message.seq ?? -1) < 0)
    throw new Error("Invalid assistant sequence.");
  if (message.seq! <= state.live.seq) return state;
  const live = event(state.live, message);
  if (message.type !== "transcript.final") return { ...state, live };
  if (
    typeof message.text !== "string" ||
    !["me", "them"].includes(String(message.channel)) ||
    !Number.isSafeInteger(message.seg_id)
  )
    throw new Error("Invalid shared transcript.");
  const id = `${message.channel}:${message.seg_id}`;
  const line = {
    id,
    text: message.text,
    speaker: message.channel === "me" ? "Host" : "Other speaker",
  };
  return {
    ...state,
    live,
    transcript: [...state.transcript.filter((t) => t.id !== id), line].slice(
      -500,
    ),
  };
}
export function seatGateway(
  raw: string,
  development = false,
  httpTest = false,
): string {
  const url = new URL(gatewayURL(raw, development, httpTest));
  url.pathname = "/v1/seat";
  return url.toString();
}
const terminalMessages: Record<number, string> = {
  1000: "The call has ended.",
  4403: "This seat is unavailable.",
  4408: "This seat is no longer open.",
  4409: "This seat is unavailable.",
  4410: "This seat is open on another device.",
  4411: "The host removed your access.",
  4412: "This invitation is no longer admitted.",
  4426: "Update the app to assist this call.",
};
export function seatClose(
  code: number,
  refreshed: boolean,
): { terminal: boolean; refresh: boolean; message: string } {
  if (code === 4401)
    return {
      terminal: refreshed,
      refresh: !refreshed,
      message: refreshed
        ? "Sign in again to assist this call."
        : "Refreshing sign-in…",
    };
  return {
    terminal: !!terminalMessages[code],
    refresh: false,
    message:
      terminalMessages[code] ?? "Reconnecting… Previous content may be stale.",
  };
}
export interface SeatSocket {
  readyState: number;
  send(value: string): void;
  close(code?: number, reason?: string): void;
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
}
export type SeatDependencies = {
  token(force?: boolean): Promise<string>;
  socket(token: string): SeatSocket;
  schedule(
    callback: () => void,
    milliseconds: number,
  ): ReturnType<typeof setTimeout>;
  cancel(timer: ReturnType<typeof setTimeout>): void;
};
/** One authorized guest socket. Pausing disconnects only the assistant, never the host call. */
export class AssistantConnection {
  state = emptyAssistant();
  private socket: SeatSocket | null = null;
  private generation = 0;
  private running = false;
  private closed = false;
  private retries = 0;
  private refreshed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private handshake: ReturnType<typeof setTimeout> | null = null;
  private seat: AssistantSeat;
  private deps: SeatDependencies;
  private change: (state: AssistantState) => void;
  constructor(
    seat: AssistantSeat,
    deps: SeatDependencies,
    change: (state: AssistantState) => void,
  ) {
    this.seat = seat;
    this.deps = deps;
    this.change = change;
  }
  private set(patch: Partial<AssistantState>) {
    if (!this.closed) {
      this.state = { ...this.state, ...patch };
      this.change(this.state);
    }
  }
  private clearTimers() {
    if (this.timer) this.deps.cancel(this.timer);
    if (this.handshake) this.deps.cancel(this.handshake);
    this.timer = null;
    this.handshake = null;
  }
  resume() {
    if (this.closed || this.running || this.state.terminal) return;
    this.running = true;
    void this.connect();
  }
  pause() {
    this.running = false;
    this.generation++;
    this.clearTimers();
    const ws = this.socket;
    this.socket = null;
    ws?.close(1000, "assistant suspended");
    this.set({
      connected: false,
      status: "Paused while this screen is away.",
      notes: this.state.notes.map((n) =>
        n.result === "pending" ? { ...n, result: "unconfirmed" } : n,
      ),
    });
  }
  close() {
    this.pause();
    this.closed = true;
    this.state = emptyAssistant();
  }
  private retry() {
    if (!this.running || this.closed || this.state.terminal) return;
    this.timer = this.deps.schedule(
      () => {
        this.timer = null;
        void this.connect();
      },
      Math.min(10000, 500 * 2 ** Math.min(this.retries++, 5)),
    );
  }
  private async connect() {
    const generation = ++this.generation;
    const current = () =>
      !this.closed && this.running && generation === this.generation;
    this.set({
      connected: false,
      status: this.retries
        ? "Reconnecting… Previous content may be stale."
        : "Connecting…",
    });
    try {
      const token = await withDeadline(
        () => this.deps.token(this.refreshed),
        15000,
      );
      if (!current()) return;
      const ws = this.deps.socket(token);
      this.socket = ws;
      const owns = () => current() && this.socket === ws;
      this.handshake = this.deps.schedule(() => {
        if (owns() && !this.state.connected) ws.close(4000, "join timeout");
      }, 15000);
      ws.onopen = () => {
        if (owns())
          ws.send(
            JSON.stringify({
              type: "seat.join",
              session_id: this.seat.session_id,
              seat_id: this.seat.seat_id,
              last_seq: this.state.live.seq,
              client_version: "mobile-0.1.0",
            }),
          );
      };
      ws.onmessage = (incoming) => {
        if (!owns() || typeof incoming.data !== "string") return;
        try {
          const message = JSON.parse(incoming.data) as Event;
          if (message.type === "auth.expiring") {
            void withDeadline(() => this.deps.token(true), 15000).then(
              (fresh) => {
                if (owns() && ws.readyState === 1)
                  ws.send(
                    JSON.stringify({
                      type: "auth.refresh",
                      access_token: fresh,
                    }),
                  );
              },
              () => {
                if (owns()) ws.close(4000, "token refresh failed");
              },
            );
            return;
          }
          if (message.type === "error") {
            this.set({
              status:
                typeof message.message === "string"
                  ? message.message
                  : "The call could not be loaded.",
            });
            return;
          }
          if (
            message.type === "seat.ready" &&
            (message.seat_id !== this.seat.seat_id ||
              message.session_id !== this.seat.session_id)
          )
            throw new Error("The server returned another call.");
          this.state = assistantEvent(this.state, message);
          this.change(this.state);
          if (message.type === "seat.ready") {
            if (this.handshake) this.deps.cancel(this.handshake);
            this.handshake = null;
            this.retries = 0;
            this.refreshed = false;
          }
        } catch {
          this.set({
            status: "The shared call could not be read. Reconnecting…",
          });
          ws.close(4000, "invalid event");
        }
      };
      ws.onerror = () => {
        if (owns()) ws.close(4000, "connection interrupted");
      };
      ws.onclose = ({ code }) => {
        if (!owns()) return;
        this.socket = null;
        if (this.handshake) this.deps.cancel(this.handshake);
        this.handshake = null;
        const result = seatClose(code, this.refreshed);
        this.refreshed = result.refresh;
        // Revoked/ended content is removed immediately instead of lingering in the hidden view.
        if (result.terminal)
          this.state = { ...emptyAssistant(), terminal: true };
        this.set({
          connected: false,
          terminal: result.terminal,
          status: result.message,
          notes: this.state.notes.map((n) =>
            n.result === "pending" ? { ...n, result: "unconfirmed" } : n,
          ),
        });
        this.retry();
      };
    } catch (error) {
      if (current()) {
        const expired =
          typeof error === "object" &&
          error !== null &&
          "status" in error &&
          error.status === 401;
        if (expired) this.state = { ...emptyAssistant(), terminal: true };
        this.set({
          connected: false,
          terminal: expired,
          status: expired
            ? "Sign in again to assist this call."
            : error instanceof Error
              ? error.message
              : "Unable to join this call.",
        });
        this.retry();
      }
    }
  }
  note(id: string, text: string) {
    const value = text.trim();
    if (
      !this.running ||
      !this.state.connected ||
      this.socket?.readyState !== 1 ||
      !this.state.capabilities.includes("note")
    )
      throw new Error("Connect to a call with note access first.");
    if (!value || value.length > 1000)
      throw new Error("Use between 1 and 1,000 characters.");
    if (this.state.notes.some((n) => n.id === id)) return;
    this.socket.send(
      JSON.stringify({
        type: "seat.command",
        command_id: id,
        kind: "note",
        text: value,
      }),
    );
    this.set({
      notes: [
        ...this.state.notes,
        { id, text: value, result: "pending" },
      ].slice(-100),
    });
  }
}
