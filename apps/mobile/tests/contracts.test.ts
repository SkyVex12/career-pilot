import test from "node:test";
import assert from "node:assert/strict";
import { Api, secureBase, type Tokens, type SecretStore } from "../src/api.ts";
import { event, emptyLive, command, gatewayURL } from "../src/liveState.ts";
import { pcmFrame, sessionURL } from "../src/pcm.ts";
function store(initial: Tokens | null = null) {
  let value = initial;
  return {
    read: async () => value,
    write: async (t: Tokens | null) => {
      value = t;
    },
    get: () => value,
  };
}
const tokens = {
  access_token: "access-secret",
  refresh_token: "refresh-secret",
  expires_in: 3600,
  email: "person@example.com",
};
test("server and gateway addresses reject plaintext, credential and token URLs", () => {
  for (const u of [
    "http://prod.example",
    "https://u:p@example.com",
    "https://example.com?token=x",
    "https://example.com#token",
  ])
    assert.throws(() => secureBase(u));
  assert.equal(secureBase("https://example.com/"), "https://example.com");
  assert.equal(secureBase("http://10.0.2.2", true), "http://10.0.2.2");
  assert.throws(() => gatewayURL("wss://example.com?token=secret"));
  assert.equal(
    sessionURL("wss://example.com/v1/companion", "p1"),
    "wss://example.com/v1/session?profile=p1",
  );
});
test("authenticated requests put account token in header and selected profile in header", async () => {
  const calls: { url: string; options?: RequestInit }[] = [];
  const api = new Api("https://example.com", store(), async (u, o) => {
    calls.push({ url: String(u), options: o });
    return new Response(JSON.stringify({ ok: true }));
  });
  await api.accept(tokens);
  api.profile = "profile-2";
  await api.request("/ask/conversations");
  assert.equal(calls[0].url, "https://example.com/v1/ask/conversations");
  assert.deepEqual(calls[0].options?.headers, {
    "Content-Type": "application/json",
    Authorization: "Bearer access-secret",
    "x-copilot-profile": "profile-2",
  });
  assert(!calls[0].url.includes("secret"));
});
test("concurrent expired tokens refresh exactly once and rotate secure credentials", async () => {
  const memory = store({ ...tokens, expires_at: 1 });
  let refreshes = 0;
  const api = new Api("https://example.com", memory, async () => {
    refreshes++;
    await new Promise((r) => setTimeout(r, 5));
    return new Response(
      JSON.stringify({
        ...tokens,
        access_token: "rotated",
        refresh_token: "new-refresh",
      }),
    );
  });
  await api.restore();
  assert.deepEqual(await Promise.all([api.token(), api.token(), api.token()]), [
    "rotated",
    "rotated",
    "rotated",
  ]);
  assert.equal(refreshes, 1);
  assert.equal(memory.get()?.refresh_token, "new-refresh");
});
test("signout revokes only this phone attachments and clears credentials", async () => {
  const calls: string[] = [];
  const memory = store();
  const api = new Api("https://example.com", memory, async (u) => {
    calls.push(String(u));
    return new Response("{}");
  });
  await api.accept(tokens);
  api.attachments.add("this-phone");
  assert.equal(await api.signout(), true);
  assert(calls.includes("https://example.com/v1/auth/token/signout"));
  assert.equal(memory.get(), null);
  assert(
    calls.includes("https://example.com/v1/companion/attachments/this-phone"),
  );
  assert(!calls.includes("https://example.com/v1/companion/attachments"));
  await assert.rejects(() => api.token());
});
test("snapshot replaces previous state and duplicate seq does not duplicate answer deltas", () => {
  let state = event(emptyLive(), {
    type: "question.detected",
    seq: 1,
    question_id: "old",
    text: "old",
  });
  state = event(state, {
    type: "companion.snapshot",
    seq: 5,
    messages: [
      { type: "question.detected", seq: 3, question_id: "new", text: "new?" },
      {
        type: "answer.delta",
        seq: 4,
        question_id: "new",
        text: "hello",
        pass: "fast",
      },
    ],
  });
  state = event(state, {
    type: "answer.delta",
    seq: 4,
    question_id: "new",
    text: "hello",
    pass: "fast",
  });
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].fast, "hello");
  assert.equal(state.seq, 5);
  assert.equal(event(state, { type: "companion.ended" }).ended, true);
});
test("ending companion requires explicit confirmation and repeat commands retain identity", () => {
  assert.throws(() => command("id", "end"));
  assert.throws(() => command("id", "followup", " "));
  assert.deepEqual(command("id", "end", "", true, false), {
    type: "companion.command",
    command_id: "id",
    kind: "end",
    confirmed: true,
    summarize: false,
  });
});
test("PCM framing matches gateway big endian header little endian signed audio and clips safely", () => {
  const data = pcmFrame(
      new Float32Array([-2, -1, 0, 1, 2, NaN]),
      1,
      3,
      0x10203040,
      1500,
    ),
    v = new DataView(data);
  assert.equal(data.byteLength, 22);
  assert.equal(v.getUint8(0), 1);
  assert.equal(v.getUint8(1), 3);
  assert.equal(v.getUint32(2, false), 0x10203040);
  assert.equal(v.getUint32(6, false), 1500);
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5].map((i) => v.getInt16(10 + 2 * i, true)),
    [-32768, -32768, 0, 32767, 32767, 0],
  );
  assert.throws(() => pcmFrame(new Float32Array(16001), 0, 0, 0, 0));
});

import { MediaEpoch } from "../src/mediaEpoch.ts";
test("suspend/signout invalidate pending microphone permission, preparation and playback", async () => {
  const gate = new MediaEpoch();
  let starts = 0;
  let release!: () => void;
  const permission = new Promise<void>((r) => {
    release = r;
  });
  const ticket = gate.begin();
  const pending = (async () => {
    await permission;
    if (gate.current(ticket)) starts++;
  })();
  gate.stop();
  release();
  await pending;
  assert.equal(starts, 0);
  assert.equal(gate.current(ticket), false);
  const explicit = gate.begin();
  assert.equal(gate.current(explicit), true);
  gate.stop();
  assert.equal(gate.current(explicit), false);
});
test("refresh completing after local signout cannot restore credentials", async () => {
  let finish!: (v: Response) => void;
  const memory = store({ ...tokens, expires_at: 1 });
  const api = new Api("https://example.com", memory, async (u) =>
    String(u).endsWith("/refresh")
      ? new Promise<Response>((r) => {
          finish = r;
        })
      : new Response("{}"),
  );
  await api.restore();
  const waiting = api.token().catch(() => null);
  await new Promise((r) => setTimeout(r, 0));
  await api.signout();
  finish(new Response(JSON.stringify(tokens)));
  assert.equal(await waiting, null);
  assert.equal(memory.get(), null);
});

test("switching profile cancels a cached read while token refresh is pending", async () => {
  let finish!: (v: Response) => void;
  const calls: RequestInit[] = [];
  const memory = store({ ...tokens, expires_at: 1 });
  const api = new Api("https://example.com", memory, async (u, o) => {
    if (String(u).endsWith("/refresh"))
      return new Promise<Response>((r) => {
        finish = r;
      });
    calls.push(o ?? {});
    return new Response("{}");
  });
  await api.restore();
  api.profile = "old-profile";
  const waiting = api.request("/sessions");
  const rejected = assert.rejects(waiting, { name: "AbortError" });
  api.profile = "new-profile";
  finish(new Response(JSON.stringify(tokens)));
  await rejected;
  assert.equal(calls.length, 0);
});

import { RequestScope } from "../src/requestScope.ts";

test("old-account 401 cannot replay a mutation with the new account token", async () => {
  let finish!: (response: Response) => void;
  const mutations: RequestInit[] = [];
  let refreshes = 0;
  const api = new Api("https://example.com", store(), async (url, options) => {
    if (String(url).endsWith("/refresh")) refreshes++;
    if (String(url).endsWith("/mutation")) {
      mutations.push(options ?? {});
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return new Response("{}");
  });
  await api.accept(tokens);
  const waiting = api.request("/mutation", { private: "old account" }, "POST");
  const rejected = assert.rejects(waiting, /Account changed/);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await api.signout();
  await api.accept({
    ...tokens,
    access_token: "new-account",
    email: "new@example.com",
  });
  finish(new Response("{}", { status: 401 }));
  await rejected;
  assert.equal(mutations.length, 1);
  assert.equal(refreshes, 0);
  assert.equal(await api.token(), "new-account");
});

test("old-account successful response cannot publish after account switch", async () => {
  let finish!: (response: Response) => void;
  const api = new Api(
    "https://example.com",
    store(),
    async () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  await api.accept(tokens);
  const waiting = api.request("/private-sources");
  const rejected = assert.rejects(waiting, /Account changed/);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await api.accept({ ...tokens, access_token: "another-account" });
  finish(new Response(JSON.stringify({ content: "previous private source" })));
  await rejected;
});

test("history selection ignores a superseded transcript even when transport ignores abort", async () => {
  const scope = new RequestScope();
  let finish!: (value: string) => void;
  let signal!: AbortSignal;
  const previous = scope.run(async (s) => {
    signal = s;
    return new Promise<string>((resolve) => {
      finish = resolve;
    });
  });
  assert.equal(
    await scope.run(async () => "call B transcript"),
    "call B transcript",
  );
  finish("call A transcript");
  assert.equal(await previous, undefined);
  assert.equal(signal.aborted, true);
});

test("back/delete/unmount invalidation cannot restore removed source content or errors", async () => {
  for (const action of ["back", "delete", "unmount"]) {
    const scope = new RequestScope();
    let finish!: (value: string) => void;
    const waiting = scope.run(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    scope.invalidate();
    finish(action);
    assert.equal(await waiting, undefined);
  }
  const scope = new RequestScope();
  let fail!: (reason: Error) => void;
  const waiting = scope.run(
    () =>
      new Promise<string>((_, reject) => {
        fail = reject;
      }),
  );
  scope.invalidate();
  fail(new Error("stale error"));
  assert.equal(await waiting, undefined);
  await assert.rejects(
    scope.run(async () => {
      throw new Error("current failure");
    }),
    /current failure/,
  );
});

test("signout queues credential deletion after an already-started secure write", async () => {
  let release!: () => void;
  let value: Tokens | null = null;
  let started!: () => void;
  const writing = new Promise<void>((resolve) => {
    started = resolve;
  });
  const memory: SecretStore = {
    read: async () => value,
    write: async (next) => {
      if (next) {
        started();
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }
      value = next;
    },
  };
  const api = new Api(
    "https://example.com",
    memory,
    async () => new Response("{}"),
  );
  const accepting = assert.rejects(api.accept(tokens), /Account changed/);
  await writing;
  const signingOut = api.signout();
  release();
  await Promise.all([accepting, signingOut]);
  assert.equal(await memory.read(), null);
  await assert.rejects(api.token(), /Sign in/);
});

test("HubRuby tokens refresh and revoke when email authentication is disabled", async () => {
  const memory = store({ ...tokens, expires_at: 1 });
  const calls: string[] = [];
  const api = new Api("https://example.com", memory, async (raw, options) => {
    const path = new URL(String(raw)).pathname;
    calls.push(path);
    if (path.startsWith("/v1/auth/email/"))
      return new Response("{}", { status: 404 });
    if (path === "/v1/auth/token/refresh") {
      assert.equal(options?.method, "POST");
      assert.deepEqual(JSON.parse(String(options?.body)), {
        refresh_token: "refresh-secret",
      });
      return Response.json({
        ...tokens,
        access_token: "career-access",
        refresh_token: "career-refresh",
      });
    }
    if (path === "/v1/auth/token/signout") {
      assert.deepEqual(JSON.parse(String(options?.body)), {
        refresh_token: "career-refresh",
      });
      return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected endpoint ${path}`);
  });
  await api.restore();
  assert.equal(await api.token(), "career-access");
  assert.equal(memory.get()?.refresh_token, "career-refresh");
  assert.equal(await api.signout(), true);
  assert.equal(memory.get(), null);
  assert.deepEqual(calls, ["/v1/auth/token/refresh", "/v1/auth/token/signout"]);
});

test("signout distinguishes an absent attachment from unconfirmed token revocation", async () => {
  for (const tokenStatus of [204, 404, 500]) {
    const memory = store();
    const api = new Api("https://example.com", memory, async (raw) => {
      const path = new URL(String(raw)).pathname;
      if (path === "/v1/companion/attachments/already-removed")
        return new Response(null, { status: 404 });
      assert.equal(path, "/v1/auth/token/signout");
      return new Response(null, { status: tokenStatus });
    });
    await api.accept(tokens);
    api.attachments.add("already-removed");
    assert.equal(await api.signout(), tokenStatus === 204);
    assert.equal(memory.get(), null);
    assert.equal(api.attachments.size, 0);
    await assert.rejects(() => api.token(), /Sign in/);
  }
});
