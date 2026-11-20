// Force transport policy from the native variant after Expo dotenv loading.
const path = require("node:path");
function prepareNativeEnvironment(mode, projectRoot) {
  if (!["http-test", "secure"].includes(mode))
    throw new Error("Missing native bundle variant.");
  process.env.NODE_ENV = "production";
  require("@expo/env").load(projectRoot);
  process.env.EXPO_PUBLIC_MOBILE_HTTP_TEST =
    mode === "http-test" ? "true" : "false";
  process.env.EXPO_NO_DOTENV = "1";
}
module.exports = { prepareNativeEnvironment };
if (require.main === module) {
  const [mode, cli, ...args] = process.argv.slice(2);
  if (!cli) throw new Error("Missing native bundle command.");
  prepareNativeEnvironment(mode, path.resolve(__dirname, ".."));
  process.argv = [process.execPath, cli, ...args];
  require(path.resolve(cli));
}
