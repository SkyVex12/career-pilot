const fs = require("node:fs");
const path = require("node:path");
const {
  AndroidConfig,
  withAndroidManifest,
  withAppBuildGradle,
  withDangerousMod,
} = require("expo/config-plugins");

const gradleLine = 'apply from: file("../../scripts/http-test.gradle")';
module.exports = function withHttpTest(config) {
  config = withAndroidManifest(config, (mod) => {
    const application = AndroidConfig.Manifest.getMainApplicationOrThrow(
      mod.modResults,
    );
    application.$["android:usesCleartextTraffic"] = "false";
    for (const activity of application.activity ?? []) {
      for (const filter of activity["intent-filter"] ?? []) {
        for (const data of filter.data ?? []) {
          if (data.$["android:scheme"] === "careerpilot")
            data.$["android:scheme"] = "${careerPilotScheme}";
          if (data.$["android:scheme"] === "exp+career-pilot-mobile")
            data.$["android:scheme"] = "${careerPilotExpoScheme}";
        }
      }
    }
    return mod;
  });
  config = withAppBuildGradle(config, (mod) => {
    if (mod.modResults.language !== "groovy")
      throw new Error(
        "HTTP test variant requires the Android Groovy template.",
      );
    if (!mod.modResults.contents.includes(gradleLine))
      mod.modResults.contents += "\n" + gradleLine + "\n";
    return mod;
  });
  return withDangerousMod(config, [
    "android",
    (mod) => {
      const root = path.join(
        mod.modRequest.platformProjectRoot,
        "app/src/httpTest",
      );
      fs.mkdirSync(path.join(root, "res/values"), { recursive: true });
      fs.writeFileSync(
        path.join(root, "AndroidManifest.xml"),
        `<manifest xmlns:android="http://schemas.android.com/apk/res/android" xmlns:tools="http://schemas.android.com/tools">
  <application android:usesCleartextTraffic="true" tools:replace="android:usesCleartextTraffic" />
</manifest>
`,
      );
      fs.writeFileSync(
        path.join(root, "res/values/strings.xml"),
        `<resources>
  <string name="app_name">CoRuby HTTP Test</string>
</resources>
`,
      );
      return mod;
    },
  ]);
};
