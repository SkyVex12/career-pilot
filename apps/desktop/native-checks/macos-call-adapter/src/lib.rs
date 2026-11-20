//! Metadata-only check of the production platform code without linking Tauri/WebKit.
//! The real app handoff is covered separately by Windows native and UI tests.
#![allow(dead_code)]
extern crate self as tauri;
#[derive(Clone)]
pub struct AppHandle;
fn background_respond(_: AppHandle, _: String, _: bool) -> Result<(), String> { Ok(()) }
#[path = "../../../src-tauri/src/call_detection.rs"]
mod detection;
#[path = "../../../src-tauri/src/call_detection_macos.rs"]
mod macos;
