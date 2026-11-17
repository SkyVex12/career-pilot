//! Guest seats' voice (ARCHITECTURE.md, "Guest seats", phase 2): a guest speaks to the host,
//! and only the host hears it. The audio itself is WebRTC in the app's window (LiveKit's
//! JavaScript SDK in WebView2: src/voice.ts); here are the rules that keep it out of the call.

pub mod isolation;
