import test from "node:test";
import assert from "node:assert/strict";
import { Api, type Tokens } from "../src/api.ts";
import {
  companionDeviceLabel,
  CompanionAttachmentRetirements,
  createCompanionIdentityReader,
  findReusableCompanionAttachment,
} from "../src/companionAttachment.ts";
const device = "11111111-1111-4111-8111-111111111111";
const otherDevice = "11111111-1111-4111-8111-111111111112";
const profile = "22222222-2222-4222-8222-222222222222";
const session = "33333333-3333-7333-8333-333333333333";
const attachment = "44444444-4444-7444-8444-444444444444";
const now = Date.parse("2026-11-20T12:00:00Z");
const expected = {
  deviceName: companionDeviceLabel("android", device),
  profileId: profile,
  sessionId: session,
};
const grant = {
  attachment_id: attachment,
  profile_id: profile,
  session_id: session,
  device_name: expected.deviceName,
  expires_at: "2026-11-20T18:00:00+00:00",
  revoked: false,
};

test("secure phone identity survives process recreation and concurrent initialization", async () => {
  let saved: string | null = null,
    reads = 0,
    writes = 0,
    generated = 0;
  const store = {
    read: async () => {
      reads++;
      return saved;
    },
    write: async (value: string) => {
      writes++;
      saved = value;
    },
  };
  const create = () => {
    generated++;
    return device;
  };
  const firstProcess = createCompanionIdentityReader(store, create);
  assert.deepEqual(
    await Promise.all([firstProcess(), firstProcess(), firstProcess()]),
    [device, device, device],
  );
  assert.equal(reads, 1);
  assert.equal(writes, 1);
  assert.equal(generated, 1);
  const nextProcess = createCompanionIdentityReader(store, create);
  assert.equal(await nextProcess(), device);
  assert.equal(reads, 2);
  assert.equal(writes, 1);
  assert.equal(generated, 1);
});
test("secure-store failures never return a temporary identity and can retry after unlocking", async () => {
  let locked = true,
    written: string | null = null,
    generated = 0;
  const read = createCompanionIdentityReader(
    {
      read: async () => {
        if (locked) throw new Error("Locked");
        return null;
      },
      write: async (value) => {
        written = value;
      },
    },
    () => {
      generated++;
      return device;
    },
  );
  await assert.rejects(read, /Locked/);
  assert.equal(generated, 0);
  assert.equal(written, null);
  locked = false;
  assert.equal(await read(), device);
  assert.equal(written, device);
  let failWrite = true;
  const write = createCompanionIdentityReader(
    {
      read: async () => null,
      write: async () => {
        if (failWrite) throw new Error("Storage unavailable");
      },
    },
    () => device,
  );
  await assert.rejects(write, /Storage unavailable/);
  failWrite = false;
  assert.equal(await write(), device);
});
test("corrupt saved identities are replaced only by a persisted valid random UUID", async () => {
  let saved = "not-a-uuid";
  const read = createCompanionIdentityReader(
    {
      read: async () => saved,
      write: async (value) => {
        saved = value;
      },
    },
    () => device,
  );
  assert.equal(await read(), device);
  assert.equal(saved, device);
  await assert.rejects(
    createCompanionIdentityReader(
      {
        read: async () => null,
        write: async () => assert.fail("invalid identity must not be stored"),
      },
      () => "bad",
    ),
    /secure phone identity/,
  );
  for (const platform of ["android", "ios", "web"]) {
    const name = companionDeviceLabel(platform, device);
    assert(name.length <= 100);
    assert(name.endsWith(device));
  }
  assert.throws(() => companionDeviceLabel("android", "short"));
});
test("reuses the same exact device, profile and session after a process restart", () => {
  assert.deepEqual(
    findReusableCompanionAttachment({ items: [grant] }, expected, now),
    grant,
  );
  assert.equal(
    findReusableCompanionAttachment({ items: [] }, expected, now),
    null,
  );
  for (const patch of [
    { device_name: companionDeviceLabel("android", otherDevice) },
    { device_name: "android phone" },
    { device_name: companionDeviceLabel("ios", device) },
    { profile_id: "55555555-5555-4555-8555-555555555555" },
    { session_id: "66666666-6666-7666-8666-666666666666" },
  ])
    assert.equal(
      findReusableCompanionAttachment(
        { items: [{ ...grant, ...patch }] },
        expected,
        now,
      ),
      null,
    );
});
test("expired, revoked and malformed rows cannot be reused", () => {
  for (const patch of [
    { expires_at: "2026-11-20T12:00:00Z" },
    { expires_at: "2026-11-20T11:59:59Z" },
    { expires_at: "2027-02-30T18:00:00Z" },
    { expires_at: "2027-01-01T25:00:00Z" },
    { expires_at: "2027-01-01T18:00:00" },
    { expires_at: 1800000000000 },
    { expires_at: {} },
    { expires_at: null },
    { expires_at: "never" },
    { revoked: true },
    { revoked: "false" },
    { revoked: undefined },
    { attachment_id: "../../other" },
    { attachment_id: {} },
    { profile_id: null },
    { device_name: { text: expected.deviceName } },
  ])
    assert.equal(
      findReusableCompanionAttachment(
        { items: [{ ...grant, ...patch }] },
        expected,
        now,
      ),
      null,
    );
  assert.deepEqual(
    findReusableCompanionAttachment(
      { items: [null, [], {}, grant] },
      expected,
      now,
    ),
    grant,
  );
  assert.equal(
    findReusableCompanionAttachment(
      { items: [grant, { ...grant, revoked: true }] },
      expected,
      now,
    ),
    null,
  );
  for (const payload of [
    null,
    {},
    { items: {} },
    { items: Array(101).fill(grant) },
  ])
    assert.throws(
      () => findReusableCompanionAttachment(payload, expected, now),
      /incompatible companion attachment list/,
    );
  assert.throws(
    () =>
      findReusableCompanionAttachment(
        { items: [] },
        { ...expected, profileId: "" },
        now,
      ),
    /identity is unavailable/,
  );
});
test("chooses the valid matching grant with the longest remaining lifetime", () => {
  const newer = {
    ...grant,
    attachment_id: "77777777-7777-7777-8777-777777777777",
    expires_at: "2026-11-20T20:00:00Z",
  };
  assert.deepEqual(
    findReusableCompanionAttachment({ items: [grant, newer] }, expected, now),
    newer,
  );
  assert.deepEqual(
    findReusableCompanionAttachment({ items: [newer, grant] }, expected, now),
    newer,
  );
});
test("an attachment response from a previous account cannot reach the reuse matcher", async () => {
  let finish!: (response: Response) => void;
  let saved: Tokens | null = null,
    matched = false;
  const api = new Api(
    "https://example.invalid",
    {
      read: async () => saved,
      write: async (value) => {
        saved = value;
      },
    },
    async () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const tokens = {
    access_token: "account-a",
    refresh_token: "refresh-a",
    email: "a@example.test",
    expires_in: 3600,
  };
  await api.accept(tokens);
  const previous = api.request("/companion/attachments").then((value) => {
    matched = true;
    return findReusableCompanionAttachment(value, expected, now);
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await api.accept({
    ...tokens,
    access_token: "account-b",
    refresh_token: "refresh-b",
    email: "b@example.test",
  });
  finish(new Response(JSON.stringify({ items: [grant] })));
  await assert.rejects(previous, /Account changed/);
  assert.equal(matched, false);
});

test("disconnecting grants cannot be reused from stale server snapshots after timeout", () => {
  const retiring = new Set<string>();
  const snapshot = { items: [grant] };
  assert.equal(
    findReusableCompanionAttachment(snapshot, expected, now)?.attachment_id,
    attachment,
  );
  // The user disconnects while DELETE is delayed; the next GET still reports a live grant.
  retiring.add(attachment);
  const match = { ...expected, excludedIds: retiring };
  assert.equal(findReusableCompanionAttachment(snapshot, match, now), null);
  // Even after the local deadline expires, a late remote DELETE may still revoke it.
  assert.equal(
    findReusableCompanionAttachment(snapshot, match, now + 8001),
    null,
  );
  const replacement = {
    ...grant,
    attachment_id: "55555555-5555-4555-8555-555555555555",
  };
  assert.equal(
    findReusableCompanionAttachment({ items: [grant, replacement] }, match, now)
      ?.attachment_id,
    replacement.attachment_id,
  );
});

test("profile A to B to A excludes a retiring grant before delayed cleanup finishes", async () => {
  let finishDelete!: (response: Response) => void;
  const replacement = {
    ...grant,
    attachment_id: "55555555-5555-4555-8555-555555555555",
  };
  const calls: string[] = [];
  const api = new Api(
    "https://example.invalid",
    { read: async () => null, write: async () => {} },
    async (input, init) => {
      const path = new URL(String(input)).pathname;
      calls.push(`${init?.method} ${path}`);
      if (init?.method === "DELETE") {
        assert.equal(path, `/v1/companion/attachments/${attachment}`);
        assert.equal(
          (init.headers as Record<string, string>)["x-copilot-profile"],
          profile,
        );
        return new Promise<Response>((resolve) => {
          finishDelete = resolve;
        });
      }
      return new Response(JSON.stringify({ items: [grant] }));
    },
  );
  await api.accept({
    access_token: "a",
    refresh_token: "r",
    email: "a@example.test",
    expires_in: 3600,
  });
  api.profile = profile;
  api.attachments.add(attachment);
  const oldMount = api.companionRetirements;
  // Unmount retirement happens synchronously, before the cleanup request yields.
  oldMount.retire(attachment);
  const cleanup = api
    .request(`/companion/attachments/${attachment}`, undefined, "DELETE")
    .then(() => api.attachments.delete(attachment));
  api.profile = "66666666-6666-4666-8666-666666666666";
  api.profile = profile;
  const newMount = api.companionRetirements;
  assert.equal(newMount, oldMount);
  const snapshot = await api.request("/companion/attachments");
  assert.equal(
    findReusableCompanionAttachment(
      snapshot,
      { ...expected, excludedIds: newMount },
      now,
    ),
    null,
  );
  // The new screen creates G2 rather than adopting G from the still-live server snapshot.
  api.attachments.add(replacement.attachment_id);
  finishDelete(new Response(null, { status: 204 }));
  await cleanup;
  assert.equal(api.attachments.has(attachment), false);
  assert.equal(api.attachments.has(replacement.attachment_id), true);
  assert.equal(
    findReusableCompanionAttachment(
      { items: [grant, replacement] },
      { ...expected, excludedIds: newMount },
      now,
    )?.attachment_id,
    replacement.attachment_id,
  );
  assert.equal(calls.filter((path) => path.startsWith("DELETE")).length, 1);
});

test("retirement scope is bounded and stops reuse instead of forgetting a late revoke", () => {
  const retired = new CompanionAttachmentRetirements();
  for (let index = 0; index < 128; index++) retired.retire(`retired-${index}`);
  assert.equal(retired.has("retired-0"), true);
  assert.equal(retired.has(attachment), false);
  retired.retire("retired-128");
  assert.equal(retired.has("retired-0"), true);
  assert.equal(
    findReusableCompanionAttachment(
      { items: [grant] },
      { ...expected, excludedIds: retired },
      now,
    ),
    null,
  );
});

test("signout and a new account discard retirement state without changing captured old scopes", async () => {
  const revoked: string[] = [];
  const api = new Api(
    "https://example.invalid",
    { read: async () => null, write: async () => {} },
    async (input, init) => {
      if (init?.method === "DELETE") revoked.push(String(input));
      return new Response(null, { status: 204 });
    },
  );
  const tokens = {
    access_token: "a",
    refresh_token: "r",
    email: "a@example.test",
    expires_in: 3600,
  };
  await api.accept(tokens);
  const oldMount = api.companionRetirements;
  api.attachments.add(attachment);
  assert.equal(await api.signout(), true);
  assert.equal(oldMount.has(attachment), true);
  assert.equal(api.companionRetirements.has(attachment), false);
  assert.deepEqual(revoked, [
    `https://example.invalid/v1/companion/attachments/${attachment}`,
  ]);
  await api.accept({ ...tokens, access_token: "b", email: "b@example.test" });
  oldMount.retire("late-old-account-response");
  assert.equal(
    api.companionRetirements.has("late-old-account-response"),
    false,
  );
  assert.equal(api.attachments.size, 0);
  const beforeReplacement = api.companionRetirements;
  beforeReplacement.retire(attachment);
  api.attachments.add(attachment);
  await api.accept({ ...tokens, access_token: "c", email: "c@example.test" });
  assert.notEqual(api.companionRetirements, beforeReplacement);
  assert.equal(api.companionRetirements.has(attachment), false);
  assert.equal(api.attachments.size, 0);
});
