//! With the `release` feature, the stage's settings (apps/desktop/release.json, or the file
//! COPILOT_RELEASE_CONFIG names) are checked here and embedded in the binary
//! (src/release.rs). Dev builds embed nothing and keep the local environment.

use std::path::PathBuf;

const REQUIRED: [&str; 4] = ["stage", "api_url", "gateway_url", "update_url"];

fn main() {
    println!("cargo:rerun-if-env-changed=COPILOT_RELEASE_CONFIG");
    if std::env::var_os("CARGO_FEATURE_RELEASE").is_some() {
        embed_release_config();
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
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("release.json");
    std::fs::write(out, text).unwrap();
}
