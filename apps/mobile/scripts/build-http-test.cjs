const { spawnSync } = require("node:child_process");
const path = require("node:path");

// Only public endpoints belong in the bundle. Tokens/passwords are entered at runtime.
const env = {
  ...process.env,
  EXPO_PUBLIC_MOBILE_HTTP_TEST: "true",
  NODE_ENV: "production",
};
const windows = process.platform === "win32";
const command = windows ? "gradlew.bat" : "./gradlew";
const args = ["assembleHttpTest", ...process.argv.slice(2)];
const result = spawnSync(command, args, {
  cwd: path.resolve(__dirname, "../android"),
  env,
  stdio: "inherit",
  shell: windows,
});
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
