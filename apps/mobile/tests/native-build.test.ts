import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const wrapper = fileURLToPath(
  new URL("../scripts/native-bundle.cjs", import.meta.url).toString(),
);
for (const mode of ["secure", "http-test"]) {
  test(`native ${mode} policy overrides conflicting dotenv settings`, () => {
    const prefix = join(tmpdir(), "career-pilot-native-env-");
    const project = mkdtempSync(prefix);
    try {
      writeFileSync(
        join(project, ".env.production.local"),
        `EXPO_PUBLIC_MOBILE_HTTP_TEST=${mode === "secure" ? "true" : "false"}\nEXPO_PUBLIC_API_URL=https://fixture.example\n`,
      );
      const env = { ...process.env };
      for (const key of [
        "EXPO_PUBLIC_MOBILE_HTTP_TEST",
        "EXPO_PUBLIC_API_URL",
        "EXPO_NO_DOTENV",
        "__EXPO_ENV_LOADED",
      ])
        delete env[key];
      const result = spawnSync(
        process.execPath,
        [
          "-e",
          `require(process.argv[1]).prepareNativeEnvironment(process.argv[2], process.argv[3]); console.log(JSON.stringify({flag:process.env.EXPO_PUBLIC_MOBILE_HTTP_TEST,api:process.env.EXPO_PUBLIC_API_URL,dotenv:process.env.EXPO_NO_DOTENV}));`,
          wrapper,
          mode,
          project,
        ],
        { env, encoding: "utf8" },
      );
      assert.equal(result.status, 0, result.stderr);
      const actual = JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1)!);
      assert.deepEqual(actual, {
        flag: mode === "http-test" ? "true" : "false",
        api: "https://fixture.example",
        dotenv: "1",
      });
    } finally {
      assert.ok(resolve(project).startsWith(resolve(prefix)));
      rmSync(project, { recursive: true, force: true });
    }
  });
}
