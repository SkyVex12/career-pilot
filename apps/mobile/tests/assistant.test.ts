import test from "node:test";
import assert from "node:assert/strict";
import {
  assistantEvent,
  AssistantConnection,
  emptyAssistant,
  parseSeats,
  seatClose,
  seatGateway,
  type AssistantSeat,
  type SeatSocket,
} from "../src/assistantState.ts";
import { controlReply, screenPoint, textKeys } from "../src/assistantScreen.ts";
const seat: AssistantSeat = {
  seat_id: "seat-a",
  session_id: "call-a",
  host_name: "Host",
  mode: "interview",
  title: null,
  status: "admitted",
  capabilities: ["view", "note"],
  expires_at: null,
};
const ready = {
  type: "seat.ready",
  seq: 10,
  seat_id: seat.seat_id,
  session_id: seat.session_id,
  capabilities: ["view", "note"],
  host_connected: true,
  sharing: true,
};
const tick = () => new Promise((resolve) => setImmediate(resolve));
class Socket implements SeatSocket {
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  send(value: string) {
    this.sent.push(JSON.parse(value));
  }
  close(code = 1000) {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({ code });
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  event(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}
function fixture(
  token: (force?: boolean) => Promise<string> = async () => "access",
) {
  const sockets: Socket[] = [],
    timers = new Map<number, () => void>();
  let id = 0;
  const client = new AssistantConnection(
    seat,
    {
      token,
      socket: (value) => {
        assert.equal(value, "access");
        const socket = new Socket();
        sockets.push(socket);
        return socket;
      },
      schedule: (callback) => {
        timers.set(++id, callback);
        return id as unknown as ReturnType<typeof setTimeout>;
      },
      cancel: (timer) => {
        timers.delete(timer as unknown as number);
      },
    },
    () => {},
  );
  return { client, sockets, timers };
}
test("seat snapshot restores merged deltas, deduplicates live events and final transcript", () => {
  let state = assistantEvent(emptyAssistant(), ready);
  state = assistantEvent(state, {
    type: "seat.snapshot",
    seq: 20,
    messages: [
      {
        type: "answer.delta",
        seq: 19,
        question_id: "q",
        pass: "fast",
        text: "Combined answer",
      },
      {
        type: "question.detected",
        seq: 4,
        question_id: "q",
        text: "Question?",
      },
      {
        type: "transcript.final",
        seq: 5,
        channel: "them",
        seg_id: 1,
        text: "Hello",
      },
    ],
  });
  assert.equal(state.live.items[0].question, "Question?");
  assert.equal(state.live.items[0].fast, "Combined answer");
  assert.equal(state.transcript.length, 1);
  assert.equal(
    assistantEvent(state, {
      type: "answer.delta",
      seq: 19,
      question_id: "q",
      text: "duplicate",
    }),
    state,
  );
  state = assistantEvent(state, {
    type: "seat.update",
    seq: 20,
    capabilities: ["view"],
  });
  assert.deepEqual(state.capabilities, ["view"]);
  state = assistantEvent(state, {
    type: "seat.host",
    seq: 20,
    connected: true,
    sharing: false,
  });
  assert.equal(state.sharing, false);
});
test("snapshot refuses nested/private messages and bad sequence", () => {
  assert.throws(() =>
    assistantEvent(emptyAssistant(), {
      type: "seat.snapshot",
      seq: 1,
      messages: [{ type: "seat.note", text: "another guest" }],
    }),
  );
  assert.throws(() =>
    assistantEvent(emptyAssistant(), { type: "answer.delta", seq: NaN }),
  );
  assert.throws(() =>
    parseSeats({ seats: [{ ...seat, capabilities: [false] }] }),
  );
});
test("native guest gateway derives endpoint without leaking auth or allowing cleartext release", () => {
  assert.equal(
    seatGateway("wss://server.example/v1/companion"),
    "wss://server.example/v1/seat",
  );
  assert.equal(
    seatGateway("ws://10.0.2.2/v1/companion", true),
    "ws://10.0.2.2/v1/seat",
  );
  assert.throws(() => seatGateway("ws://server.example/v1/companion"));
  assert.throws(() => seatGateway("wss://server.example/v1/companion?token=x"));
});
test("invitation join sends only seat message, notes await real acknowledgement and revocation clears private content", async () => {
  const f = fixture();
  f.client.resume();
  await tick();
  const socket = f.sockets[0];
  socket.open();
  assert.equal(socket.sent[0].type, "seat.join");
  assert.equal(socket.sent.length, 1);
  socket.event(ready);
  f.client.note("note-1", "  Private note  ");
  f.client.note("note-1", "Duplicate");
  assert.equal(socket.sent.length, 2);
  assert.equal(f.client.state.notes[0].result, "pending");
  socket.event({
    type: "seat.command.result",
    command_id: "note-1",
    seq: 10,
    result: "ok",
  });
  assert.equal(f.client.state.notes[0].result, "ok");
  socket.event({
    type: "question.detected",
    seq: 11,
    question_id: "q",
    text: "Secret",
  });
  socket.close(4411);
  assert.equal(f.client.state.terminal, true);
  assert.equal(f.client.state.live.items.length, 0);
  assert.equal(f.client.state.notes.length, 0);
  assert.equal(f.timers.size, 0);
  assert.throws(() => f.client.note("note-2", "Forbidden"));
  f.client.close();
});
test("background/signout invalidates pending auth and cannot start a late socket", async () => {
  let resolve!: (value: string) => void;
  const f = fixture(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  f.client.resume();
  f.client.close();
  resolve("access");
  await tick();
  assert.equal(f.sockets.length, 0);
  assert.equal(f.timers.size, 0);
});
test("reconnect replays from last sequence; a lost note is never automatically resubmitted", async () => {
  const f = fixture();
  f.client.resume();
  await tick();
  const first = f.sockets[0];
  first.open();
  first.event(ready);
  first.event({
    type: "transcript.final",
    seq: 18,
    channel: "me",
    seg_id: 2,
    text: "Answer",
  });
  f.client.note("n", "note");
  first.close(1006);
  assert.equal(f.client.state.notes[0].result, "unconfirmed");
  const retry = [...f.timers.values()][0];
  f.timers.clear();
  retry();
  await tick();
  const second = f.sockets[1];
  second.open();
  assert.equal(second.sent[0].last_seq, 18);
  assert.equal(second.sent.length, 1);
  f.client.close();
});
test("one refresh retry and terminal seat codes stop; host cannot be ended by leave", () => {
  assert.deepEqual(seatClose(4401, false), {
    terminal: false,
    refresh: true,
    message: "Refreshing sign-in…",
  });
  assert.equal(seatClose(4401, true).terminal, true);
  for (const code of [1000, 4408, 4410, 4411, 4412, 4426])
    assert.equal(seatClose(code, false).terminal, true);
  assert.equal(seatClose(4500, false).terminal, false);
});
test("only matching host approval grants control; revocation always wins", () => {
  const asked = { requestId: "request-a", controlling: false };
  assert.deepEqual(
    controlReply(asked, "seat:attacker", { on: true, requestId: "request-a" }),
    asked,
  );
  assert.deepEqual(
    controlReply(asked, "host", { on: true, requestId: "old" }),
    asked,
  );
  assert.deepEqual(
    controlReply(asked, "host", { on: true, requestId: "request-a" }),
    { controlling: true, requestId: null },
  );
  assert.deepEqual(
    controlReply({ controlling: true, requestId: null }, "host", { on: false }),
    { controlling: false, requestId: null },
  );
});
test("touch is normalized within the rendered picture; text creates balanced key strokes", () => {
  assert.deepEqual(screenPoint(50, 25, 100, 50), { x: 0.5, y: 0.5 });
  assert.equal(screenPoint(-1, 0, 100, 50), null);
  const held = new Set();
  for (const event of textKeys("Hello @ 100%\n")) {
    if (event.t === "key_down") held.add(event.code);
    if (event.t === "key_up") {
      assert.ok(held.has(event.code));
      held.delete(event.code);
    }
  }
  assert.equal(held.size, 0);
  assert.throws(() => textKeys("中文"), /US English/);
  assert.throws(() => textKeys("x".repeat(501)), /500/);
});

import { AssistantAudioLease } from "../src/assistantAudioLease.ts";
import { command, emptyLive, event } from "../src/liveState.ts";
test("native audio lease serializes teardown and stale room cleanup cannot mute the new owner", async () => {
  const operations: string[] = [];
  const audio = new AssistantAudioLease(
    async () => {
      operations.push("start");
    },
    async () => {
      operations.push("stop");
    },
  );
  const old = {},
    next = {};
  await audio.start(old, () => true);
  await audio.start(next, () => true);
  await audio.stop(old);
  assert.deepEqual(operations, ["start", "stop", "start"]);
  await audio.stop(next);
  assert.deepEqual(operations, ["start", "stop", "start", "stop"]);
});
test("cancelled native audio startup is cleaned up before the next room begins", async () => {
  let resolve!: () => void,
    valid = true;
  let starts = 0,
    stops = 0;
  const audio = new AssistantAudioLease(
    async () => {
      if (++starts === 1)
        await new Promise<void>((r) => {
          resolve = r;
        });
    },
    async () => {
      stops++;
    },
  );
  const initial = audio.start({}, () => valid);
  await tick();
  valid = false;
  const next = audio.start({}, () => true);
  resolve();
  await initial;
  await next;
  assert.equal(starts, 2);
  assert.equal(stops, 1);
});
test("sharing controls require advertised capability and authoritative events survive snapshot", () => {
  let state = event(emptyLive(), {
    type: "companion.ready",
    sharing_control: true,
    sharing: false,
  });
  assert.equal(state.sharingControl, true);
  assert.equal(state.assistantSharing, false);
  state = event(state, { type: "companion.snapshot", seq: 10, messages: [] });
  assert.equal(state.assistantSharing, false);
  state = event(state, { type: "sharing", seq: 12, on: true });
  assert.equal(state.assistantSharing, true);
  state = event(state, { type: "sharing", seq: 11, on: false });
  assert.equal(state.assistantSharing, true);
  assert.deepEqual(command("same-id", "set_sharing", "", false, true, false), {
    type: "companion.command",
    command_id: "same-id",
    kind: "set_sharing",
    sharing: false,
  });
  assert.equal(emptyLive().sharingControl, false);
});

test("expired credentials immediately clear previous call data and stop reconnecting", async () => {
  const f = fixture(async () => {
    throw Object.assign(new Error("expired"), { status: 401 });
  });
  f.client.state = assistantEvent(emptyAssistant(), {
    type: "transcript.final",
    seq: 1,
    channel: "them",
    seg_id: 1,
    text: "Private call",
  });
  f.client.resume();
  await tick();
  assert.equal(f.client.state.terminal, true);
  assert.equal(f.client.state.transcript.length, 0);
  assert.equal(f.timers.size, 0);
  assert.equal(f.sockets.length, 0);
});
test("notes respect the shared protocol's 1,000 character limit", async () => {
  const f = fixture();
  f.client.resume();
  await tick();
  f.sockets[0].open();
  f.sockets[0].event(ready);
  assert.throws(() => f.client.note("long", "x".repeat(1001)), /1,000/);
  f.client.note("max", "x".repeat(1000));
  assert.equal(f.sockets[0].sent.at(-1)?.kind, "note");
  f.client.close();
});

test("starting in-person capture drains assistant playback before acquiring its audio session", async () => {
  const log: string[] = [];
  const audio = new AssistantAudioLease(
    async () => {
      log.push("listen");
    },
    async () => {
      log.push("stop");
    },
  );
  const owner = {};
  await audio.start(owner, () => true);
  await audio.quiesce();
  log.push("record");
  await audio.stop(owner);
  assert.deepEqual(log, ["listen", "stop", "record"]);
});

test("a stale request-specific decline cannot erase a later control request or grant", () => {
  const pending = { requestId: "new-request", controlling: false };
  const declined = controlReply(pending, "host", {
    on: false,
    requestId: "old-request",
  });
  assert.equal(declined, pending);
  const granted = controlReply(declined, "host", {
    on: true,
    requestId: "new-request",
  });
  assert.equal(granted.controlling, true);
  assert.equal(
    controlReply(granted, "host", { on: false, requestId: "old-request" }),
    granted,
  );
  assert.deepEqual(
    controlReply(pending, "host", { on: false, requestId: "new-request" }),
    { requestId: null, controlling: false },
  );
  assert.deepEqual(controlReply(granted, "host", { on: false }), {
    requestId: null,
    controlling: false,
  });
});
