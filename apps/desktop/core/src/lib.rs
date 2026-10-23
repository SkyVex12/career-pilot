//! Desktop client core (ARCHITECTURE.md, "Desktop client"): audio capture, echo cancellation,
//! VAD, the gateway WebSocket and the Copilot API, in Rust so the web UI only renders. The Tauri app (`apps/desktop/src-tauri`) wires these to
//! windows, commands and the global hotkey.

pub mod api;
pub mod audio;
pub mod auth;
pub mod engine;
pub mod latency;
pub mod net;
pub mod oauth;

pub use copilot_protocol as protocol;
