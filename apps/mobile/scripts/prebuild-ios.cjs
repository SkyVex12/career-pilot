// Generate an iOS native project on Windows without invoking CocoaPods or Xcode.
// Expo's CLI skips iOS on Windows; these same template/config steps are platform-neutral.
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const cli = path.join(
  path.dirname(require.resolve("expo/package.json")),
  "node_modules/@expo/cli",
);
const { getConfig } = require("@expo/config");
const { updateFromTemplateAsync } = require(
  path.join(cli, "build/src/prebuild/updateFromTemplate.js"),
);
const { configureProjectAsync } = require(
  path.join(cli, "build/src/prebuild/configureProjectAsync.js"),
);
(async () => {
  const { exp, pkg } = getConfig(root);
  const result = await updateFromTemplateAsync(root, {
    exp,
    pkg,
    platforms: ["ios"],
    skipDependencyUpdate: ["react", "react-native"],
  });
  await configureProjectAsync(root, {
    platforms: ["ios"],
    exp,
    templateChecksum: result.templateChecksum,
  });
  console.log(
    "iOS native project generated; CocoaPods, Xcode build and signing require macOS.",
  );
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
