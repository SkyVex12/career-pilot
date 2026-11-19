//! With the `cloud` feature (also included by `release`), the stage's settings
//! (apps/desktop/release.json, or the file
//! COPILOT_RELEASE_CONFIG names) are checked here and embedded in the binary
//! (src/release.rs). Other builds embed nothing and keep the local environment.

use std::path::PathBuf;

const REQUIRED: [&str; 4] = ["stage", "api_url", "gateway_url", "update_url"];

fn main() {
    // Windows embeds the icon as a PE resource, outside Rust's include_bytes!
    // dependency tracking. Recompile it when the shared ruby export changes.
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-env-changed=COPILOT_RELEASE_CONFIG");
    if std::env::var_os("CARGO_FEATURE_CLOUD").is_some() {
        embed_release_config();
    }
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        build_mac_screen();
    }
    tauri_build::build()
}

fn embed_release_config() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let path = std::env::var_os("COPILOT_RELEASE_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|| manifest.join("../release.json"));
    println!("cargo:rerun-if-changed={}", path.display());
    let text =
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("release config {}: {e}", path.display()));
    let value: serde_json::Value =
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("release config {}: {e}", path.display()));
    for key in REQUIRED {
        let ok = value.get(key).and_then(|v| v.as_str()).is_some_and(|s| !s.trim().is_empty());
        assert!(ok, "release config {}: `{key}` is missing or empty", path.display());
    }
    // URLs with `{host}` (the on-demand VM's address) need the wake function that gives it.
    let needs_wake = ["api_url", "gateway_url"]
        .iter()
        .any(|k| value.get(*k).and_then(|v| v.as_str()).is_some_and(|s| s.contains("{host}")));
    let has_wake = value.get("wake_url").and_then(|v| v.as_str()).is_some_and(|s| s.starts_with("https://"));
    assert!(
        !needs_wake || has_wake,
        "release config {}: `{{host}}` in the URLs needs an https `wake_url`",
        path.display()
    );
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("release.json");
    std::fs::write(out, text).unwrap();
}

fn build_mac_screen() {
    use std::process::Command;
    println!("cargo:rerun-if-changed=native/screen.m");
    println!("cargo:rerun-if-changed=native/capture_mask.m");
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let sdk = Command::new("xcrun")
        .args(["--sdk", "macosx", "--show-sdk-path"])
        .output()
        .expect("Xcode Command Line Tools are required");
    assert!(sdk.status.success(), "could not find macOS SDK");
    let sdk = String::from_utf8(sdk.stdout).unwrap();
    let arch =
        if std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("aarch64") { "arm64" } else { "x86_64" };
    let mut objects = Vec::new();
    for source in ["screen", "capture_mask"] {
        let object = out.join(format!("copilot_{source}.o"));
        let status = Command::new("xcrun")
            .args([
                "clang",
                "-fobjc-arc",
                "-fblocks",
                "-arch",
                arch,
                "-mmacosx-version-min=15.4",
                "-isysroot",
                sdk.trim(),
                "-c",
            ])
            .arg(format!("native/{source}.m"))
            .arg("-o")
            .arg(&object)
            .status()
            .expect("clang is required");
        assert!(status.success(), "macOS {source} bridge compilation failed");
        objects.push(object);
    }
    let status = Command::new("xcrun")
        .args(["ar", "crs"])
        .arg(out.join("libcopilot_screen.a"))
        .args(&objects)
        .status()
        .expect("ar is required");
    assert!(status.success(), "ScreenCaptureKit archive failed");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=copilot_screen");
    println!("cargo:rustc-link-lib=objc");
    for framework in [
        "AppKit",
        "AVFoundation",
        "QuartzCore",
        "Foundation",
        "ScreenCaptureKit",
        "CoreImage",
        "CoreMedia",
        "CoreVideo",
        "CoreGraphics",
        "ImageIO",
    ] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }
}
