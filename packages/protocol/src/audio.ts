// Binary audio frame header (AudioFrameHeader in protocol.schema.json).
// Layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 BE, [6..9] capture_ms uint32 BE, then PCM.
import type { AudioFrameHeader } from "./generated.ts";

export const HEADER_SIZE = 10;
export const MAX_FRAME_BYTES = 64 * 1024;

export const CHANNEL_THEM = 0;
export const CHANNEL_ME = 1;
export const FLAG_SPEECH_START = 0b01;
export const FLAG_SPEECH_END = 0b10;

const UINT32_MAX = 0xffff_ffff;

function checkHeader(h: AudioFrameHeader): void {
  if (h.channel !== CHANNEL_THEM && h.channel !== CHANNEL_ME) {
    throw new RangeError(`channel must be 0 or 1, got ${String(h.channel)}`);
  }
  if (!Number.isInteger(h.flags) || h.flags < 0 || h.flags > 3) {
    throw new RangeError(`flags must be 0..3, got ${h.flags}`);
  }
  for (const key of ["frame_seq", "capture_ms"] as const) {
    const v = h[key];
    if (!Number.isInteger(v) || v < 0 || v > UINT32_MAX) {
      throw new RangeError(`${key} must be a uint32, got ${v}`);
    }
  }
}

export function encodeFrame(header: AudioFrameHeader, audio: Uint8Array = new Uint8Array()): Uint8Array {
  checkHeader(header);
  const frame = new Uint8Array(HEADER_SIZE + audio.byteLength);
  if (frame.byteLength > MAX_FRAME_BYTES) {
    throw new RangeError(`audio frame is ${frame.byteLength} bytes; the limit is ${MAX_FRAME_BYTES}`);
  }
  const view = new DataView(frame.buffer);
  view.setUint8(0, header.channel);
  view.setUint8(1, header.flags);
  view.setUint32(2, header.frame_seq, false);
  view.setUint32(6, header.capture_ms, false);
  frame.set(audio, HEADER_SIZE);
  return frame;
}

export function decodeFrame(frame: Uint8Array): { header: AudioFrameHeader; audio: Uint8Array } {
  if (frame.byteLength < HEADER_SIZE) {
    throw new RangeError(`audio frame is ${frame.byteLength} bytes; the header alone is ${HEADER_SIZE}`);
  }
  if (frame.byteLength > MAX_FRAME_BYTES) {
    throw new RangeError(`audio frame is ${frame.byteLength} bytes; the limit is ${MAX_FRAME_BYTES}`);
  }
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  const header = {
    channel: view.getUint8(0),
    flags: view.getUint8(1),
    frame_seq: view.getUint32(2, false),
    capture_ms: view.getUint32(6, false),
  } as AudioFrameHeader;
  checkHeader(header);
  return { header, audio: frame.subarray(HEADER_SIZE) };
}
