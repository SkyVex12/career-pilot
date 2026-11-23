import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, type Api } from "../src/api.ts";
import { updateAssistantPermissions } from "../src/hostAssistantPermissions.ts";

test("a phone's stale note change cannot restore voice revoked on desktop", async () => {
  const cached = ["view", "note", "voice"];
  let stored = ["view", "note"],
    refreshes = 0,
    attempts = 0;
  const api: Pick<Api, "request"> = {
    async request<T>(path: string, raw?: unknown, method?: string): Promise<T> {
      attempts++;
      assert.equal(path, "/sessions/call-a/seats/seat-a");
      assert.equal(method, "PATCH");
      const body = raw as {
        capabilities: string[];
        expected_capabilities: string[];
      };
      if (
        JSON.stringify(body.expected_capabilities.slice().sort()) !==
        JSON.stringify(stored.slice().sort())
      )
        throw new ApiError(
          409,
          "Assistant permissions changed. Review the current access and try again.",
        );
      stored = body.capabilities;
      return { capabilities: stored } as T;
    },
  };
  await assert.rejects(
    updateAssistantPermissions(
      api,
      "call-a",
      "seat-a",
      cached,
      cached.filter((c) => c !== "note"),
      () => refreshes++,
    ),
    (error: unknown) => error instanceof ApiError && error.status === 409,
  );
  assert.equal(attempts, 1, "a conflict is never automatically retried");
  assert.equal(refreshes, 1, "the roster refreshes for the host to review");
  assert.deepEqual(stored, ["view", "note"], "revoked voice remains revoked");
  await updateAssistantPermissions(
    api,
    "call-a",
    "seat-a",
    stored,
    ["view"],
    () => refreshes++,
  );
  assert.deepEqual(
    stored,
    ["view"],
    "a new explicit change uses the refreshed access",
  );
  assert.deepEqual(
    cached,
    ["view", "note", "voice"],
    "the original snapshot was not mutated",
  );
  assert.equal(refreshes, 1);
});

test("non-conflict permission errors remain visible without retrying or refreshing", async () => {
  const denied = new ApiError(403, "Host access changed");
  let calls = 0,
    refreshes = 0;
  const api: Pick<Api, "request"> = {
    async request<T>(): Promise<T> {
      calls++;
      throw denied;
    },
  };
  await assert.rejects(
    updateAssistantPermissions(
      api,
      "call-a",
      "seat-a",
      ["view"],
      ["view", "voice"],
      () => refreshes++,
    ),
    (error) => error === denied,
  );
  assert.equal(calls, 1);
  assert.equal(refreshes, 0);
});
