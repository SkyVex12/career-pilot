import test from "node:test";
import assert from "node:assert/strict";
import {
  checkClientCapabilities,
  CLIENT_CONTRACTS,
  parseClientCapabilities,
  supportsClientContract,
} from "../src/clientCapabilities.ts";

const manifest = {
  schema_version: 1,
  server_version: "0.0.0",
  contracts: { ...CLIENT_CONTRACTS },
};

test("only recognized advertised contracts produce compatibility, without claiming runtime support", () => {
  const result = parseClientCapabilities(manifest);
  assert.equal(result.state, "compatible");
  assert.equal(result.notice, null);
  assert.equal(result.serverVersion, "0.0.0");
  assert.equal(supportsClientContract(result, "languages"), true);
  assert.equal("voice_available" in result, false);
  assert.equal("credentials_configured" in result, false);
  assert.equal("proxy_available" in result, false);
});

test("missing and older features give useful upgrade guidance while retaining known contracts", () => {
  const result = parseClientCapabilities({
    ...manifest,
    contracts: { companion: 1, ask: 0 },
  });
  assert.equal(result.state, "upgrade_required");
  assert.match(result.notice!, /Update the CoRuby server/);
  assert.match(result.notice!, /Ask/);
  assert.equal(supportsClientContract(result, "companion"), true);
  assert.equal(supportsClientContract(result, "ask"), false);
  assert.equal(supportsClientContract(result, "practice"), false);
});

test("malformed success payloads cannot masquerade as a compatible server", () => {
  for (const value of [
    null,
    [],
    {},
    { status: "ok" },
    { ...manifest, contracts: [] },
    { ...manifest, contracts: { ...CLIENT_CONTRACTS, practice: true } },
    { ...manifest, contracts: { ...CLIENT_CONTRACTS, ask: NaN } },
    { ...manifest, contracts: { ...CLIENT_CONTRACTS, ask: -1 } },
  ]) {
    const result = parseClientCapabilities(value);
    assert.equal(result.state, "unverified");
    assert.equal(supportsClientContract(result, "practice"), false);
  }
  assert.equal(
    parseClientCapabilities({ ...manifest, schema_version: 2 }).state,
    "app_update_required",
  );
  assert.equal(
    parseClientCapabilities({ ...manifest, schema_version: 0 }).state,
    "upgrade_required",
  );
});

test("compatibility uses a single authenticated nonretrying request", async () => {
  const calls: unknown[][] = [];
  const api = {
    request: async <T>(...args: unknown[]): Promise<T> => {
      calls.push(args);
      return manifest as T;
    },
  };
  assert.equal((await checkClientCapabilities(api)).state, "compatible");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(0, 4), [
    "/client-capabilities",
    undefined,
    "GET",
    true,
  ]);
  assert.equal(calls[0][5], false);
  assert(calls[0][4] instanceof AbortSignal);
});

test("404 older servers and unavailable/auth errors are not reported as successful support", async () => {
  for (const [status, state] of [
    [404, "upgrade_required"],
    [401, "signin_required"],
    [403, "unverified"],
    [426, "app_update_required"],
    [503, "unverified"],
  ] as const) {
    let requests = 0;
    const api = {
      request: async <T>(): Promise<T> => {
        requests++;
        throw Object.assign(new Error("private server details"), { status });
      },
    };
    const result = await checkClientCapabilities(api);
    assert.equal(result.state, state);
    assert.equal(supportsClientContract(result, "companion"), false);
    assert.equal(requests, 1);
    assert(!result.notice!.includes("private server details"));
  }
});

test("deadline settles even when transport ignores cancellation", async () => {
  let active: AbortSignal | undefined;
  const api = {
    request: async <T>(
      _path: string,
      _body?: unknown,
      _method?: string,
      _auth?: boolean,
      signal?: AbortSignal,
    ): Promise<T> => {
      active = signal;
      return new Promise(() => {});
    },
  };
  const result = await checkClientCapabilities(api, undefined, 10);
  assert.equal(result.state, "unverified");
  assert.match(result.notice!, /taking too long/);
  assert.equal(active?.aborted, true);
});

test("a caller cannot extend the compatibility check beyond four seconds", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let signal: AbortSignal | undefined;
  const api = {
    request: async <T>(
      _path: string,
      _body?: unknown,
      _method?: string,
      _auth?: boolean,
      bounded?: AbortSignal,
    ): Promise<T> => {
      signal = bounded;
      return new Promise(() => {});
    },
  };
  const pending = checkClientCapabilities(api, undefined, 60000);
  context.mock.timers.tick(3999);
  assert.equal(signal?.aborted, false);
  context.mock.timers.tick(1);
  assert.equal((await pending).state, "unverified");
  assert.equal(signal?.aborted, true);
});

test("account/server cancellation propagates instead of publishing a stale notice", async () => {
  const controller = new AbortController();
  let requests = 0;
  const api = {
    request: async <T>(): Promise<T> => {
      requests++;
      return new Promise(() => {});
    },
  };
  const pending = checkClientCapabilities(api, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(requests, 1);
  await assert.rejects(checkClientCapabilities(api, controller.signal), {
    name: "AbortError",
  });
  assert.equal(requests, 1);
});
