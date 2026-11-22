import assert from "node:assert/strict";
import test from "node:test";
import { gatewayOverride, resolveConnection } from "../src/connectionConfig.ts";

test("changing a restored workspace derives its own gateway instead of reusing server A", () => {
  const saved = resolveConnection("https://server-a.example/v1", "");
  const editable = gatewayOverride(saved.api, saved.gateway);
  assert.equal(editable, "");
  assert.deepEqual(
    resolveConnection("https://server-b.example/v1/", editable),
    {
      api: "https://server-b.example",
      gateway: "wss://server-b.example/v1/companion",
    },
  );
  // The same editable state remains safe after cancelling automatic restoration.
  assert.equal(
    resolveConnection("http://10.0.2.2:8765", editable, false, true).gateway,
    "ws://10.0.2.2:8765/v1/companion",
  );
});

test("intentional custom gateways remain explicit across workspace changes and can be cleared", () => {
  const saved = resolveConnection(
    "https://server-a.example",
    "wss://gateway.example/live",
  );
  const editable = gatewayOverride(saved.api, saved.gateway);
  assert.equal(editable, "wss://gateway.example/live");
  assert.equal(
    resolveConnection("https://server-b.example", editable).gateway,
    editable,
  );
  assert.equal(
    resolveConnection("https://server-b.example", "").gateway,
    "wss://server-b.example/v1/companion",
  );
});

test("malformed overrides stay visible and are rejected, while equivalent automatic URLs normalize", () => {
  assert.equal(
    gatewayOverride(
      "https://SERVER-A.example:443/v1/",
      "wss://server-a.example/v1/companion",
    ),
    "",
  );
  const invalid = "wss://server-a.example/v1/companion?secret=hidden";
  assert.equal(gatewayOverride("https://server-a.example", invalid), invalid);
  assert.throws(() => resolveConnection("https://server-b.example", invalid));
  assert.throws(() => resolveConnection("http://server-b.example", ""));
});
