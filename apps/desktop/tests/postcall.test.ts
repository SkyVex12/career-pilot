// The post-call screen's polling and formatting (src/postcall.ts).
import { describe, expect, it } from "vitest";
import type { SessionDetail } from "../src/api";
import { POLL_TIMEOUT_MS, clock, outputsInOrder, pollSession, transcriptRows, waitingText, type PollState } from "../src/postcall";

const ID = "0192f3c1-7b2e-7a10-9c41-5d2e8f0a1b33";

function session(over: Partial<SessionDetail> = {}): SessionDetail {
  return { id: ID, mode: "interview", status: "processing", outputs: {}, questions: 2, store_transcript: true, me_audio_status: "pending", ...over };
}

/// A fake clock: sleeping advances it.
function fakeTime() {
  let t = 0;
  return { now: () => t, sleep: async (ms: number) => void (t += ms) };
}

describe("pollSession", () => {
  it("polls until the session is ready", async () => {
    const replies = [session({ status: "live" }), session(), session({ me_audio_status: "uploaded" }), session({ status: "ready", outputs: { summary: "Done." } })];
    const seen: PollState[] = [];
    const time = fakeTime();
    const final = await pollSession(async () => replies.shift()!, (s) => seen.push(s), { intervalMs: 3000, ...time });
    expect(final.phase).toBe("ready");
    expect(final.phase === "ready" && final.session.outputs.summary).toBe("Done.");
    expect(seen.map((s) => s.phase)).toEqual(["waiting", "waiting", "waiting", "ready"]);
    expect(time.now()).toBe(9000);
  });

  it("gives up after five minutes", async () => {
    const time = fakeTime();
    let calls = 0;
    const final = await pollSession(async () => (calls++, session()), () => undefined, time);
    expect(final.phase).toBe("timeout");
    expect(time.now()).toBe(POLL_TIMEOUT_MS);
    expect(calls).toBe(POLL_TIMEOUT_MS / 3000 + 1);
  });

  it("keeps polling through errors and shows them", async () => {
    const time = fakeTime();
    const replies: (SessionDetail | Error)[] = [new Error("could not reach the API"), session({ status: "ready" })];
    const seen: PollState[] = [];
    const final = await pollSession(
      async () => {
        const r = replies.shift()!;
        if (r instanceof Error) throw r;
        return r;
      },
      (s) => seen.push(s),
      time,
    );
    expect(final.phase).toBe("ready");
    expect(seen[0].phase === "waiting" && seen[0].error).toContain("could not reach");
  });

  it("stops when cancelled", async () => {
    let n = 0;
    const final = await pollSession(async () => session(), () => undefined, { ...fakeTime(), cancelled: () => ++n > 2 });
    expect(final.phase).toBe("waiting");
  });
});

describe("post-call formatting", () => {
  it("orders outputs by mode", () => {
    const outputs = { follow_up_email: "Hi", feedback: "Good", summary: "S" };
    expect(outputsInOrder(session({ outputs })).map(([k]) => k)).toEqual(["summary", "feedback", "follow_up_email"]);
    const standup = session({ mode: "standup", outputs: { summary: "S", standup_report: "R" } });
    expect(outputsInOrder(standup).map(([k]) => k)).toEqual(["standup_report", "summary"]);
  });

  it("interleaves the transcript by time", () => {
    const rows = transcriptRows([
      { channel: "me", seg_id: 0, start_ms: 4_000, end_ms: 6_000, text: "I led it." },
      { channel: "them", seg_id: 0, start_ms: 1_000, end_ms: 3_000, text: "Tell me about the migration?" },
      { channel: "them", seg_id: 1, start_ms: 3_725_000, end_ms: 3_726_000, text: "Thanks." },
    ]);
    expect(rows.map((r) => `${r.at} ${r.who}`)).toEqual(["0:01 Them", "0:04 You", "1:02:05 Them"]);
    expect(clock(65_000)).toBe("1:05");
  });

  it("says what it is waiting for", () => {
    const waiting = (s: SessionDetail): PollState => ({ phase: "waiting", session: s, elapsedMs: 0, error: null });
    expect(waitingText(waiting(session({ status: "live" })), true)).toMatch(/Ending/);
    // Both sides are transcribed live: nothing waits for an audio upload.
    expect(waitingText(waiting(session({ me_audio_status: "live" })), true)).toBe("Writing the summary…");
    expect(waitingText(waiting(session({ me_audio_status: "none" })), false)).toMatch(/summary/);
    expect(waitingText({ phase: "timeout", session: null, error: null }, true)).toMatch(/5 minutes/);
  });
});
