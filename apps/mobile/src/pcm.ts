/** Existing wire contract: 10 byte big-endian header and little-endian mono PCM16. */
export function pcmFrame(
  samples: Float32Array,
  channel: 0 | 1,
  flags: number,
  seq: number,
  captureMs: number,
): ArrayBuffer {
  if (
    samples.length > 16000 ||
    !Number.isInteger(seq) ||
    seq < 0 ||
    seq > 0xffffffff
  )
    throw new Error("Invalid audio frame");
  const data = new ArrayBuffer(10 + samples.length * 2),
    view = new DataView(data);
  view.setUint8(0, channel);
  view.setUint8(1, flags);
  view.setUint32(2, seq, false);
  view.setUint32(
    6,
    Math.max(0, Math.min(0xffffffff, Math.round(captureMs))),
    false,
  );
  for (let i = 0; i < samples.length; i++) {
    const value = Math.max(
      -1,
      Math.min(1, Number.isFinite(samples[i]) ? samples[i] : 0),
    );
    view.setInt16(
      10 + i * 2,
      Math.round(value < 0 ? value * 32768 : value * 32767),
      true,
    );
  }
  return data;
}
export function sessionURL(companion: string, profile: string) {
  const u = new URL(companion);
  if (u.protocol !== "wss:" || u.username || u.password || u.search || u.hash)
    throw new Error("Use a secure gateway address without credentials.");
  u.pathname = "/v1/session";
  if (profile) u.searchParams.set("profile", profile);
  return u.toString();
}
