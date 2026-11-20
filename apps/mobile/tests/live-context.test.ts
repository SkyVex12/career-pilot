import test from "node:test";
import assert from "node:assert/strict";
import { emptyLive, event } from "../src/liveState.ts";
test("private context notices survive snapshot replay and stale updates cannot replace them", () => {
  const state = event(emptyLive(), {
    type: "companion.snapshot",
    seq: 8,
    messages: [
      {
        type: "status",
        seq: 7,
        service: "memory",
        message: "Using private brief version 3",
      },
    ],
  });
  assert.equal(state.memory, "Using private brief version 3");
  assert.equal(
    event(state, {
      type: "status",
      seq: 6,
      service: "memory",
      message: "Old version",
    }).memory,
    state.memory,
  );
});
