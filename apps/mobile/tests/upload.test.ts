import test from "node:test";
import assert from "node:assert/strict";
import { Api, type Tokens } from "../src/api.ts";
const tokens = {
  access_token: "fixture-access",
  refresh_token: "fixture-refresh",
  expires_in: 3600,
  email: "test@example.invalid",
};
function store() {
  let value: Tokens | null = null;
  return {
    read: async () => value,
    write: async (t: Tokens | null) => {
      value = t;
    },
  };
}

test("raw file uploads preserve exact bytes, filename and original profile across token retry", async () => {
  const calls: { url: string; options: RequestInit }[] = [];
  const api = new Api(
    "https://example.invalid",
    store(),
    async (url, options) => {
      calls.push({ url: String(url), options: options! });
      if (String(url).endsWith("/auth/token/refresh"))
        return new Response(
          JSON.stringify({ ...tokens, access_token: "rotated" }),
        );
      if (calls.length === 1) {
        api.profile = "another-profile";
        return new Response("{}", { status: 401 });
      }
      return new Response(JSON.stringify({ id: "uploaded" }));
    },
  );
  await api.accept(tokens);
  api.profile = "original-profile";
  const bytes = new Uint8Array([0, 10, 127, 255]);
  const pending = api.upload(
    "/employers/company/documents",
    bytes,
    "text/markdown",
    "Résumé 日本語.md",
  );
  bytes.fill(5);
  assert.deepEqual(await pending, { id: "uploaded" });
  for (const call of [calls[0], calls[2]]) {
    const headers = call.options.headers as Record<string, string>;
    assert.equal(headers["Content-Type"], "text/markdown");
    assert.equal(headers["x-copilot-profile"], "original-profile");
    assert.equal(
      decodeURIComponent(headers["x-file-name"]),
      "Résumé 日本語.md",
    );
    assert.deepEqual(
      [...new Uint8Array(call.options.body as ArrayBuffer)],
      [0, 10, 127, 255],
    );
    assert(!call.url.includes("fixture-access"));
  }
});
test("invalid upload payloads fail without sending a request", async () => {
  let count = 0;
  const api = new Api("https://example.invalid", store(), async () => {
    count++;
    return new Response("{}");
  });
  await api.accept(tokens);
  for (const [data, type, name] of [
    ["", "text/plain", "empty.txt"],
    ["text", "image/png", "file.png"],
    ["text", "text/plain", "bad\r\nname"],
    ["text", "text/plain", "bad\tname"],
    ["text", "text/plain", "x".repeat(256)],
  ] as const)
    await assert.rejects(() => api.upload("/documents", data, type, name));
  await assert.rejects(() =>
    api.upload(
      "/documents",
      new Uint8Array(10 * 1024 * 1024 + 1),
      "application/pdf",
      "large.pdf",
    ),
  );
  assert.equal(count, 0);
});
test("canceling a pending token acquisition never later sends the file", async () => {
  let finish!: (r: Response) => void;
  const paths: string[] = [];
  const api = new Api("https://example.invalid", store(), async (url) => {
    paths.push(String(url));
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  await api.accept({ ...tokens, expires_in: 0 });
  const controller = new AbortController();
  const uploading = api.upload(
    "/employers/company/documents",
    "draft",
    "text/plain",
    "draft.txt",
    controller.signal,
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  controller.abort();
  await assert.rejects(() => uploading, { name: "AbortError" });
  finish(new Response(JSON.stringify(tokens)));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(paths, ["https://example.invalid/v1/auth/token/refresh"]);
});
test("late upload response cannot publish into a different signed-in account", async () => {
  let finish!: (r: Response) => void;
  const api = new Api(
    "https://example.invalid",
    store(),
    async () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await api.accept(tokens);
  const uploading = api.upload(
    "/employers/company/documents",
    "content",
    "text/plain",
    "doc.txt",
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  await api.accept({ ...tokens, email: "other@example.invalid" });
  finish(new Response("{}"));
  await assert.rejects(() => uploading, /Account changed/);
});
