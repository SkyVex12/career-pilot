import test from "node:test";
import assert from "node:assert/strict";
import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { Api, type Tokens } from "../src/api.ts";
import {
  cacheLifetime,
  hasPendingContent,
  ResponseCache,
  type ResponsePersistence,
  type StoredResponse,
} from "../src/dataCache.ts";
import { ProtectedResponseCache } from "../src/protectedCache.ts";
import { preparationPending } from "../src/applicationData.ts";

const tokens = {
  access_token: "access",
  refresh_token: "refresh",
  expires_in: 3600,
  email: "test@example.invalid",
};
const scope = {
  "X-Copilot-Account": "account-a",
  "X-Copilot-Workspace": "workspace-a",
};
const json = (data: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(data), { headers });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function client(
  fetcher: typeof fetch,
  persistence?: ResponsePersistence,
) {
  let saved: Tokens | null = null;
  const api = new Api(
    "https://example.invalid",
    {
      read: async () => saved,
      write: async (value) => {
        saved = value;
      },
    },
    fetcher,
    false,
    false,
    1000,
    persistence,
  );
  await api.accept(tokens);
  return api;
}
function disk() {
  const entries = new Map<string, StoredResponse>();
  let writes = 0;
  return {
    entries,
    get writes() {
      return writes;
    },
    read: async (namespace: string, path: string) =>
      entries.get(namespace + path) ?? null,
    write: async (namespace: string, path: string, data: StoredResponse) => {
      writes++;
      entries.set(namespace + path, data);
    },
    remove: async (namespace: string, path: string) => {
      entries.delete(namespace + path);
    },
    clear: async () => {
      entries.clear();
    },
  };
}
test("repeat visits and simultaneous readers share one response without sharing mutable objects", async () => {
  let calls = 0,
    finish!: (response: Response) => void;
  const api = await client(async () => {
    calls++;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const a = api.request<{ employers: { name: string }[] }>("/employers"),
    b = api.request("/employers");
  await tick();
  finish(json({ employers: [{ name: "Saved" }] }));
  const first = await a;
  await b;
  first.employers[0].name = "Unsent draft";
  assert.deepEqual(await api.request("/employers"), {
    employers: [{ name: "Saved" }],
  });
  assert.equal(calls, 1);
  assert.equal(api.cacheStats.shared, 1);
  assert.equal(api.cacheStats.hits, 1);
});
test("one cancelled subscriber cannot cancel another screen, while the last subscriber releases transport", async () => {
  let finish!: (response: Response) => void, nativeSignal!: AbortSignal;
  const api = await client(async (_url, options) => {
    nativeSignal = options!.signal!;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const cancelled = new AbortController(),
    other = new AbortController();
  const a = api.request("/employers", undefined, "GET", true, cancelled.signal),
    rejected = assert.rejects(a, { name: "AbortError" });
  const b = api.request("/employers", undefined, "GET", true, other.signal);
  await tick();
  cancelled.abort();
  await rejected;
  assert.equal(nativeSignal.aborted, false);
  finish(json({ employers: [] }));
  await b;
  api.invalidateCache();
  const last = new AbortController(),
    c = api.request("/employers", undefined, "GET", true, last.signal),
    stopped = assert.rejects(c, { name: "AbortError" });
  await tick();
  last.abort();
  await stopped;
  assert.equal(nativeSignal.aborted, true);
});
test("stale resources send validators and reuse 304 bodies without parsing an empty response", async () => {
  const headers: Headers[] = [];
  let calls = 0;
  const api = await client(async (_url, options) => {
    headers.push(new Headers(options?.headers));
    calls++;
    return calls === 1
      ? json(
          { sessions: [] },
          { ETag: '"v1"', "Cache-Control": "private, max-age=0" },
        )
      : new Response(null, {
          status: 304,
          headers: { "Cache-Control": "private, max-age=30" },
        });
  });
  assert.deepEqual(await api.request("/sessions"), { sessions: [] });
  assert.deepEqual(await api.request("/sessions"), { sessions: [] });
  assert.equal(headers[1].get("If-None-Match"), '"v1"');
  await api.request("/sessions");
  assert.equal(calls, 2);
  assert.equal(api.cacheStats.unchanged, 1);
});
test("live/access/side-effect reads and no-store responses never enter the cache", async () => {
  const persistence = disk();
  let calls = 0;
  const api = await client(async () => {
    calls++;
    return json(
      { status: "ready" },
      { ...scope, ETag: '"x"', "Cache-Control": "no-store" },
    );
  }, persistence);
  for (const path of [
    "/sessions/live",
    "/seats",
    "/profiles",
    "/practice/sessions/x",
    "/practice/sessions/x/speech",
    "/client-capabilities",
    "/employers",
    "/sessions/x/transcript",
  ]) {
    await api.request(path);
    await api.request(path);
  }
  assert.equal(calls, 16);
  assert.equal(persistence.writes, 0);
  assert.equal(cacheLifetime("/sessions/live"), null);
  assert.equal(hasPendingContent({ sessions: [{ status: "live" }] }), true);
});
test("caller reload bypasses freshness and caller no-store cannot replace a cached value", async () => {
  let calls = 0;
  const api = await client(async () => json({ value: ++calls }));
  assert.deepEqual(await api.request("/employers"), { value: 1 });
  assert.deepEqual(
    await api.request(
      "/employers",
      undefined,
      "GET",
      true,
      undefined,
      true,
      api.profile,
      undefined,
      "no-store",
    ),
    { value: 2 },
  );
  assert.deepEqual(await api.request("/employers"), { value: 1 });
  assert.deepEqual(
    await api.request(
      "/employers",
      undefined,
      "GET",
      true,
      undefined,
      true,
      api.profile,
      undefined,
      "reload",
    ),
    { value: 3 },
  );
});

test("usage always reads the server even when an older server advertises cache freshness", async () => {
  let reads = 0;
  const persistence = disk();
  const api = await client(async () => json(
    { measured_seconds: ++reads },
    { ...scope, "Cache-Control": "private, max-age=60" },
  ), persistence);
  assert.equal(cacheLifetime("/usage"), null);
  assert.equal(cacheLifetime("/usage?month=2026-11"), null);
  assert.deepEqual(await api.request("/usage"), { measured_seconds: 1 });
  assert.deepEqual(await api.request("/usage"), { measured_seconds: 2 });
  assert.equal(persistence.writes, 0);
});
test("writes invalidate dependencies and a delayed older read cannot repopulate them", async () => {
  let finish!: (response: Response) => void,
    reads = 0;
  const api = await client(async (_url, options) => {
    if (options?.method === "PATCH") return json({ name: "New" });
    reads++;
    if (reads === 1)
      return new Promise((resolve) => {
        finish = resolve;
      });
    return json({ employers: [{ name: "New" }] });
  });
  const old = api.request("/employers"),
    rejected = assert.rejects(old, { name: "AbortError" });
  await tick();
  await api.request("/employers/id", { name: "New" }, "PATCH");
  finish(json({ employers: [{ name: "Old" }] }));
  await rejected;
  assert.deepEqual(await api.request("/employers"), {
    employers: [{ name: "New" }],
  });
  assert.equal(reads, 2);
});
test("account and profile transitions fence late reads and never serve the old namespace", async () => {
  let finish!: (response: Response) => void,
    calls = 0;
  const api = await client(async () => {
    calls++;
    return calls === 1
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : json({ account: "new" });
  });
  const old = api.request("/sessions"),
    rejected = assert.rejects(old);
  await tick();
  await api.accept({ ...tokens, email: "new@example.invalid" });
  finish(json({ account: "old" }));
  await rejected;
  assert.deepEqual(await api.request("/sessions"), { account: "new" });
  api.profile = "shared";
  await api.request("/sessions");
  assert.equal(calls, 3);
});
test("pending results are not fresh hits and missing/expired packs never imply running work", async () => {
  let calls = 0;
  const api = await client(async () => {
    calls++;
    return json({
      applications: [
        { pack: { available: false, operation_state: "running" } },
      ],
    });
  });
  await api.request("/applications");
  await api.request("/applications");
  assert.equal(calls, 2);
  for (const pack of [
    null,
    { available: false, operation_state: "idle" as const },
    { available: false, operation_state: "failed" as const },
  ])
    assert.equal(preparationPending({ pack }), false);
  assert.equal(
    preparationPending({
      pack: { available: false, operation_state: "queued" },
    }),
    true,
  );
});
test("concurrent status consumers share transport but each later status check is fresh", async () => {
  let calls = 0,
    finish!: (response: Response) => void;
  const api = await client(async () => {
    calls++;
    return calls === 1
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : json({ pending: false }, { "Cache-Control": "private, no-store" });
  });
  const a = api.request("/applications/status"),
    b = api.request("/applications/status");
  await tick();
  finish(json({ pending: true }, { "Cache-Control": "private, no-store" }));
  await Promise.all([a, b]);
  assert.equal(calls, 1);
  assert.deepEqual(await api.request("/applications/status"), {
    pending: false,
  });
  assert.equal(calls, 2);
});
test("private disk bodies require fresh authorization and conditional validation on restart", async () => {
  const persistence = disk();
  const first = await client(
    async () =>
      json(
        { status: "ready", private: "history" },
        { ...scope, ETag: '"saved"', "Cache-Control": "private, max-age=30" },
      ),
    persistence,
  );
  await first.request("/sessions/call");
  await tick();
  assert.equal(persistence.writes, 1);
  let calls = 0;
  const second = await client(async (url, options) => {
    calls++;
    if (String(url).endsWith("/profiles")) return json({ items: [] }, scope);
    assert.equal(new Headers(options?.headers).get("If-None-Match"), '"saved"');
    return new Response(null, {
      status: 304,
      headers: { ...scope, "Cache-Control": "private, max-age=30" },
    });
  }, persistence);
  await second.request("/profiles");
  assert.deepEqual(await second.request("/sessions/call"), {
    status: "ready",
    private: "history",
  });
  assert.equal(calls, 2);
  const offline = await client(async (url) => {
    if (String(url).endsWith("/profiles")) return json({}, scope);
    throw new Error("offline");
  }, persistence);
  await offline.request("/profiles");
  await assert.rejects(offline.request("/sessions/call"), /offline/);
});
test("public catalog persists within server freshness without requiring private offline access", async () => {
  const persistence = disk();
  const first = await client(
    async () =>
      json(
        { languages: ["en"] },
        { "Cache-Control": "private, max-age=86400" },
      ),
    persistence,
  );
  await first.request("/call-languages");
  await tick();
  let calls = 0;
  const second = await client(async () => {
    calls++;
    throw new Error("offline");
  }, persistence);
  assert.deepEqual(await second.request("/call-languages"), {
    languages: ["en"],
  });
  assert.equal(calls, 0);
});
test("foreground delta checks are throttled and invalidations cover worker changes and deletions", async () => {
  let syncs = 0,
    reads = 0;
  const api = await client(async (url) => {
    if (String(url).includes("/sync?")) {
      syncs++;
      return json({
        reset: syncs === 1,
        cursor: String(syncs),
        changes:
          syncs === 2
            ? [{ resource: "sessions", id: "deleted", deleted: true }]
            : [],
        has_more: false,
      });
    }
    return json({ sessions: [{ id: ++reads }] });
  });
  await api.syncDomain("history");
  await api.request("/sessions");
  await api.syncDomain("history");
  await api.request("/sessions");
  assert.equal(syncs, 1);
  assert.equal(reads, 1);
  await api.syncDomain("history", undefined, true);
  await api.request("/sessions");
  assert.equal(syncs, 2);
  assert.equal(reads, 2);
});
test("memory cache obeys LRU entry and byte bounds", () => {
  const cache = new ResponseCache(15, 2),
    row = (path: string) => ({
      path,
      data: path,
      checkedAt: 0,
      ttl: 1,
      bytes: 5,
    });
  cache.set("a", row("a"));
  cache.set("b", row("b"));
  cache.get("a");
  cache.set("c", row("c"));
  assert.equal(cache.get("b"), undefined);
  assert(cache.get("a"));
  cache.set("huge", { ...row("huge"), bytes: 20 });
  assert.equal(cache.get("huge"), undefined);
});
test("logout fences new reads immediately even while the protected cache purge is still running", async () => {
  let finish!: () => void,
    calls = 0;
  const persistence = {
    ...disk(),
    clear: () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  };
  const api = await client(async () => {
    calls++;
    return json({});
  }, persistence);
  const signingOut = api.signout();
  await assert.rejects(api.request("/sessions"), /Sign in/);
  assert.equal(calls, 0);
  finish();
  await signingOut;
});
test("protected persistence stores authenticated ciphertext, isolates namespaces, and serializes purge after writes", async () => {
  const key = randomBytes(32);
  let contents: Uint8Array | null = null;
  const io = {
    read: async () => contents,
    write: async (bytes: Uint8Array) => {
      contents = bytes;
    },
    clear: async () => {
      contents = null;
    },
    encrypt: async (bytes: Uint8Array) => {
      const iv = randomBytes(12),
        cipher = createCipheriv("aes-256-gcm", key, iv);
      return Buffer.concat([
        iv,
        cipher.update(bytes),
        cipher.final(),
        cipher.getAuthTag(),
      ]);
    },
    decrypt: async (bytes: Uint8Array) => {
      const buffer = Buffer.from(bytes),
        decipher = createDecipheriv("aes-256-gcm", key, buffer.subarray(0, 12));
      decipher.setAuthTag(buffer.subarray(-16));
      return Buffer.concat([
        decipher.update(buffer.subarray(12, -16)),
        decipher.final(),
      ]);
    },
  };
  const persistence = new ProtectedResponseCache(io, 4096),
    value = { data: { private: "career secret" }, savedAt: Date.now() };
  await persistence.write("account-1", "/sessions/one", value);
  assert.equal(Buffer.from(contents!).includes("career secret"), false);
  const restored = new ProtectedResponseCache(io, 4096);
  assert.deepEqual(await restored.read("account-1", "/sessions/one"), value);
  assert.equal(await restored.read("account-2", "/sessions/one"), null);
  const writing = restored.write("account-1", "/sessions/two", value),
    clearing = restored.clear();
  await Promise.all([writing, clearing]);
  assert.equal(contents, null);
});
