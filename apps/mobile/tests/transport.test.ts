import test from "node:test";
import assert from "node:assert/strict";
import { Api, secureBase } from "../src/api.ts";
import { gatewayURL } from "../src/liveState.ts";
import { sessionURL } from "../src/pcm.ts";

const hosts = [
  "localhost",
  "127.0.0.1",
  "10.0.2.2",
  "192.168.1.10",
  "test.example",
];
for (const [label, development, httpTest] of [
  ["production", false, false],
  ["development", true, false],
  ["HTTP test", false, true],
] as const) {
  test(`${label} applies the same cleartext rules to REST, companion and in-person`, () => {
    for (const host of hosts) {
      const allowed =
        httpTest || (development && hosts.slice(0, 3).includes(host));
      const calls = [
        () => secureBase(`http://${host}:8000/v1`, development, httpTest),
        () =>
          gatewayURL(`ws://${host}:8001/v1/companion`, development, httpTest),
        () =>
          sessionURL(
            `ws://${host}:8001/v1/companion`,
            "p1",
            development,
            httpTest,
          ),
      ];
      for (const call of calls)
        allowed ? assert.doesNotThrow(call) : assert.throws(call);
      assert.equal(
        secureBase(`https://${host}/v1/`, development, httpTest),
        `https://${host}`,
      );
      assert.equal(
        gatewayURL(`wss://${host}/v1/companion`, development, httpTest),
        `wss://${host}/v1/companion`,
      );
      assert.equal(
        sessionURL(`wss://${host}/v1/companion`, "p1", development, httpTest),
        `wss://${host}/v1/session?profile=p1`,
      );
    }
  });
}

test("HTTP test mode never permits credential, query, fragment or wrong-protocol addresses", () => {
  for (const suffix of ["?token=secret", "?", "#token", "#"]) {
    assert.throws(() =>
      secureBase(`http://test.example${suffix}`, false, true),
    );
    assert.throws(() => gatewayURL(`ws://test.example${suffix}`, false, true));
    assert.throws(() =>
      sessionURL(`ws://test.example${suffix}`, "p1", false, true),
    );
  }
  for (const authority of ["user:pass@test.example", "@test.example"]) {
    assert.throws(() => secureBase(`http://${authority}`, false, true));
    assert.throws(() => gatewayURL(`ws://${authority}`, false, true));
    assert.throws(() => sessionURL(`ws://${authority}`, "p1", false, true));
  }
  for (const raw of [
    "ftp://test.example",
    "file:///tmp/example",
    "javascript:alert(1)",
  ]) {
    assert.throws(() => secureBase(raw, false, true));
    assert.throws(() => gatewayURL(raw, false, true));
    assert.throws(() => sessionURL(raw, "p1", false, true));
  }
  assert.throws(() => secureBase("wss://test.example", false, true));
  assert.throws(() => gatewayURL("https://test.example", false, true));
});

test("REST normalizes /v1 once and test APK sends actual requests to the HTTP origin", async () => {
  const requests: string[] = [];
  const store = { read: async () => null, write: async () => {} };
  const api = new Api(
    "http://test.example:8000/v1/",
    store,
    async (url) => {
      requests.push(String(url));
      return new Response('{"ok":true}');
    },
    false,
    true,
  );
  await api.request("/auth/config", undefined, "GET", false);
  assert.deepEqual(requests, ["http://test.example:8000/v1/auth/config"]);
  assert.throws(
    () => new Api("http://test.example", store, fetch, false, false),
  );
  for (const path of ["/api", "/v1/auth", "/v10"])
    assert.throws(() => secureBase(`https://test.example${path}`), /origin/);
  const url = new URL(
    sessionURL(
      "ws://test.example/v1/companion",
      "profile&token=x",
      false,
      true,
    ),
  );
  assert.equal(url.searchParams.get("profile"), "profile&token=x");
  assert.equal(url.searchParams.has("token"), false);
});
