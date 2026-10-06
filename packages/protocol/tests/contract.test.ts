// Contract tests: shared fixtures round-trip through the generated TypeScript types.
// The same fixtures run through the Python types in test_contract.py.
import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";

import { decodeFrame, encodeFrame } from "../src/audio.ts";
import type { AudioFrameHeader, ClientMessage, ServerMessage } from "../src/index.ts";

const read = (path: string): unknown => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));

interface Schema {
  $id: string;
  $defs: Record<string, { oneOf?: { $ref: string }[]; properties?: { type?: { const: string } } }>;
}
interface Fixtures {
  client: Record<string, unknown>[];
  server: Record<string, unknown>[];
  invalid: { side: "client" | "server"; why: string; message: unknown }[];
  audio_frames: { why: string; header: AudioFrameHeader; audio_hex: string; frame_hex: string }[];
  invalid_audio_frames: { why: string; frame_hex: string }[];
}

const schema = read("../protocol.schema.json") as Schema;
const fixtures = read("../fixtures/messages.json") as Fixtures;

const ajv = new Ajv2020({ discriminator: true, allErrors: true });
addFormats.default(ajv);
ajv.addKeyword("x-binary-layout");
ajv.addSchema(schema);
const validateClient = ajv.compile<ClientMessage>({ $ref: `${schema.$id}#/$defs/ClientMessage` });
const validateServer = ajv.compile<ServerMessage>({ $ref: `${schema.$id}#/$defs/ServerMessage` });
const validators = { client: validateClient, server: validateServer };

const hex = (bytes: Uint8Array): string => Buffer.from(bytes).toString("hex");
const unhex = (s: string): Uint8Array => new Uint8Array(Buffer.from(s, "hex"));

function assertNever(x: never): never {
  throw new Error(`unhandled message: ${JSON.stringify(x)}`);
}

// Exhaustive over the generated unions: adding a message type to the schema without
// handling it here fails `pnpm typecheck`, and every field read must exist on the type.
function clientSummary(m: ClientMessage): string {
  switch (m.type) {
    case "session.start":
      return `${m.mode} ${m.platform} ${m.client_version} ${m.audio.sample_rate} ${m.consent.store_transcript}`;
    case "session.resume":
      return `${m.session_id} ${m.last_seq}`;
    case "auth.refresh":
      return m.access_token;
    case "question.force":
      return m.text ?? "(hotkey)";
    case "screenshot":
      return `${m.image.length} ${m.question_id ?? ""}`;
    case "answer.expand":
      return m.question_id;
    case "answer.feedback":
      return `${m.question_id} ${m.rating} ${m.reason ?? ""}`;
    case "mode.set":
      return m.mode;
    case "session.end":
      return "end";
    default:
      return assertNever(m);
  }
}

function serverSummary(m: ServerMessage): string {
  switch (m.type) {
    case "session.ready":
      return `${m.seq} ${m.session_id} ${m.server_time} ${m.limits.max_minutes}`;
    case "transcript.partial":
      return `${m.seq} ${m.channel} ${m.seg_id} ${m.text}`;
    case "transcript.final":
      return `${m.seq} ${m.channel} ${m.seg_id} ${m.start_ms}-${m.end_ms} ${m.text}`;
    case "question.detected":
      return `${m.seq} ${m.question_id} ${m.question_type} ${m.confidence} ${m.speculative}`;
    case "question.cancelled":
      return `${m.seq} ${m.question_id}`;
    case "answer.delta":
      return `${m.seq} ${m.pass} ${m.text}`;
    case "answer.done":
      return `${m.seq} ${m.pass} ${m.memory_refs.map((r) => r.node_id).join(",")} ${m.usage.output_tokens}`;
    case "answer.error":
      return `${m.seq} ${m.pass} ${m.code}`;
    case "status":
      return `${m.seq} ${m.component} ${m.state} ${m.message}`;
    case "auth.expiring":
      return `${m.seq} ${m.expires_in_s}`;
    case "error":
      return `${m.seq} ${m.code} ${m.fatal} ${m.retry_after_ms ?? ""}`;
    default:
      return assertNever(m);
  }
}

function wireTypes(side: "Client" | "Server"): string[] {
  return (schema.$defs[`${side}Message`]?.oneOf ?? []).map((ref) => {
    const name = ref.$ref.split("/").pop() ?? "";
    return schema.$defs[name]?.properties?.type?.const ?? "";
  });
}

describe("client messages", () => {
  it("has a fixture for every message type", () => {
    expect(new Set(fixtures.client.map((m) => m.type))).toEqual(new Set(wireTypes("Client")));
  });
  it.each(fixtures.client.map((m, i) => [`${i}-${String(m.type)}`, m] as const))(
    "round-trips %s",
    (_id, fixture) => {
      const wire = JSON.stringify(fixture);
      const parsed: unknown = JSON.parse(wire);
      if (!validateClient(parsed)) throw new Error(ajv.errorsText(validateClient.errors));
      const msg: ClientMessage = parsed;
      expect(clientSummary(msg)).toBeTruthy();
      expect(JSON.parse(JSON.stringify(msg))).toEqual(fixture);
    },
  );
});

describe("server messages", () => {
  it("has a fixture for every message type", () => {
    expect(new Set(fixtures.server.map((m) => m.type))).toEqual(new Set(wireTypes("Server")));
  });
  it.each(fixtures.server.map((m, i) => [`${i}-${String(m.type)}`, m] as const))(
    "round-trips %s",
    (_id, fixture) => {
      const parsed: unknown = JSON.parse(JSON.stringify(fixture));
      if (!validateServer(parsed)) throw new Error(ajv.errorsText(validateServer.errors));
      const msg: ServerMessage = parsed;
      expect(serverSummary(msg)).toBeTruthy();
      expect(JSON.parse(JSON.stringify(msg))).toEqual(fixture);
    },
  );
});

describe("invalid messages", () => {
  it.each(fixtures.invalid.map((c) => [c.why, c] as const))("rejects: %s", (_why, c) => {
    expect(validators[c.side](c.message)).toBe(false);
  });
});

describe("audio frames", () => {
  it.each(fixtures.audio_frames.map((c) => [c.why, c] as const))("round-trips: %s", (_why, c) => {
    const audio = unhex(c.audio_hex);
    const frame = encodeFrame(c.header, audio);
    expect(hex(frame)).toBe(c.frame_hex);
    const decoded = decodeFrame(frame);
    expect(decoded.header).toEqual(c.header);
    expect(hex(decoded.audio)).toBe(c.audio_hex);
  });
  it.each(fixtures.invalid_audio_frames.map((c) => [c.why, c] as const))("rejects: %s", (_why, c) => {
    expect(() => decodeFrame(unhex(c.frame_hex))).toThrow(RangeError);
  });
  it("enforces the 64 KB frame limit", () => {
    const header: AudioFrameHeader = { channel: 0, flags: 0, frame_seq: 0, capture_ms: 0 };
    expect(encodeFrame(header, new Uint8Array(64 * 1024 - 10)).byteLength).toBe(64 * 1024);
    expect(() => encodeFrame(header, new Uint8Array(64 * 1024 - 9))).toThrow(RangeError);
  });
});
