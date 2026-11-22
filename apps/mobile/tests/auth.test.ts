import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { parseAuthConfig, loadAuthConfig } from "../src/authConfig.ts";
import {
  beginCareerOs,
  completeCareerOs,
  CAREER_OS_REDIRECT,
} from "../src/careerOs.ts";
import type { Api } from "../src/api.ts";
const option = {
  authorize_url: "https://career.example/career-pilot/authorize",
  client_id: "career-pilot",
};
const crypto = {
  randomBytes: (n: number) => Uint8Array.from({ length: n }, (_, i) => i),
  sha256: async (s: string) => createHash("sha256").update(s).digest("hex"),
};
function client() {
  const calls: unknown[][] = [];
  return {
    calls,
    api: {
      request: async (...args: unknown[]) => {
        calls.push(args);
        return { status: "signed_in" };
      },
    } as unknown as Api,
  };
}
function callback(attempt: { authorizeUrl: string }, code = "one-time") {
  return `${CAREER_OS_REDIRECT}?code=${code}&state=${new URL(attempt.authorizeUrl).searchParams.get("state")}`;
}
test("current stage configuration offers HubRuby and does not invent email availability", async () => {
  const config = parseAuthConfig({
    provider: "google",
    career_os: option,
    client_secret: "not-retained",
  });
  assert.equal(config.email, null);
  assert.deepEqual(config.career_os, option);
  assert.equal("client_secret" in config, false);
  assert.throws(() =>
    parseAuthConfig({
      provider: "google",
      career_os: { ...option, authorize_url: "http://career.example" },
    }),
  );
  const { api, calls } = client();
  await assert.rejects(() => loadAuthConfig(api));
  assert.deepEqual(calls[0], [
    "/auth/config",
    undefined,
    "GET",
    false,
    undefined,
  ]);
});
test("PKCE browser URL uses existing desktop redirect and one-use unauthenticated exchange", async () => {
  const attempt = await beginCareerOs(option, crypto);
  const url = new URL(attempt.authorizeUrl);
  assert.equal(url.searchParams.get("redirect_uri"), CAREER_OS_REDIRECT);
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("code_challenge")?.length, 43);
  const { api, calls } = client();
  await attempt.exchange(api, callback(attempt));
  assert.equal(calls[0][0], "/auth/career-os/sign-in");
  assert.equal(calls[0][3], false);
  const body = calls[0][1] as Record<string, string>;
  assert.equal(body.code_verifier.length, 43);
  assert.equal(body.redirect_uri, CAREER_OS_REDIRECT);
  await assert.rejects(
    () => attempt.exchange(api, callback(attempt)),
    /already used/,
  );
  assert.equal(calls.length, 1);
});
test("callback refuses wrong origin/path/state, duplicate parameters and fragments before transport", async () => {
  const attempt = await beginCareerOs(option, crypto);
  const valid = callback(attempt);
  const { api, calls } = client();
  for (const bad of [
    valid.replace("127.0.0.1", "evil.example"),
    valid.replace("/callback", "/other"),
    valid.replace("state=", "state=wrong"),
    valid + "&code=second",
    valid + "#fragment",
    valid.replace("127.0.0.1", "user@127.0.0.1"),
  ])
    await assert.rejects(() => attempt.exchange(api, bad));
  assert.equal(calls.length, 0);
  await attempt.exchange(api, valid);
  assert.equal(calls.length, 1);
});
test("expiry cancellation denial and failed exchange cannot replay a code", async () => {
  let now = 0;
  const expired = await beginCareerOs(option, crypto, () => now);
  now = 300001;
  const { api, calls } = client();
  await assert.rejects(
    () => expired.exchange(api, callback(expired)),
    /expired/,
  );
  const cancelled = await beginCareerOs(option, crypto);
  cancelled.cancel();
  await assert.rejects(
    () => cancelled.exchange(api, callback(cancelled)),
    /expired/,
  );
  const denied = await beginCareerOs(option, crypto);
  await assert.rejects(
    () =>
      denied.exchange(
        api,
        callback(denied).replace("code=one-time", "error=access_denied"),
      ),
    /declined/,
  );
  await assert.rejects(
    () => denied.exchange(api, callback(denied)),
    /already used/,
  );
  const failed = await beginCareerOs(option, crypto);
  const broken = {
    request: async () => {
      throw new Error("offline");
    },
  } as unknown as Api;
  await assert.rejects(
    () => failed.exchange(broken, callback(failed)),
    /offline/,
  );
  await assert.rejects(
    () => failed.exchange(api, callback(failed)),
    /already used/,
  );
  assert.equal(calls.length, 0);
});
test("existing account linking requires authenticated request proof", async () => {
  const { api, calls } = client();
  await completeCareerOs(api, "t".repeat(24), "existing", "account");
  assert.equal(calls[0][3], true);
  assert.deepEqual(calls[0][1], {
    ticket: "t".repeat(24),
    choice: "existing",
    account_id: "account",
  });
  await assert.rejects(() =>
    completeCareerOs(api, "t".repeat(24), "new", "account"),
  );
});
