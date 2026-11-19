//! Compile the Ventura-compatible audio bridge only for Mac targets.
fn main() {
    println!("cargo:rerun-if-changed=native/audio.m");
    println!("cargo:rerun-if-env-changed=MACOSX_DEPLOYMENT_TARGET");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }
    use std::path::PathBuf;
    use std::process::Command;
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let sdk = Command::new("xcrun")
        .args(["--sdk", "macosx", "--show-sdk-path"])
        .output()
        .expect("Xcode Command Line Tools are required");
    assert!(sdk.status.success(), "could not find macOS SDK");
    let sdk = String::from_utf8(sdk.stdout).unwrap();
    let arch =
        if std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("aarch64") { "arm64" } else { "x86_64" };
    let minimum = std::env::var("MACOSX_DEPLOYMENT_TARGET").unwrap_or_else(|_| "13.0".into());
    let object = out.join("copilot_audio.o");
    let status = Command::new("xcrun")
        .args(["clang", "-fobjc-arc", "-fblocks", "-Werror=unguarded-availability", "-arch", arch])
        .arg(format!("-mmacosx-version-min={minimum}"))
        .args(["-isysroot", sdk.trim(), "-c", "native/audio.m", "-o"])
        .arg(&object)
        .status()
        .expect("clang is required");
    assert!(status.success(), "macOS audio bridge compilation failed");
    let status = Command::new("xcrun")
        .args(["ar", "crs"])
        .arg(out.join("libcopilot_audio.a"))
        .arg(object)
        .status()
        .expect("ar is required");
    assert!(status.success(), "macOS audio bridge archive failed");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=copilot_audio");
    println!("cargo:rustc-link-lib=objc");
    for framework in [
        "Foundation",
        "AVFoundation",
        "AudioToolbox",
        "CoreAudio",
        "CoreMedia",
        "CoreGraphics",
        "ScreenCaptureKit",
    ] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }
}
