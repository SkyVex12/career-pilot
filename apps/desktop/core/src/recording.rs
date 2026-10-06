//! Hook for recording the user's own audio (ARCHITECTURE.md, "Session lifecycle and resume").
//!
//! Live, the mic channel sends only speech timing. The design records the user's audio
//! locally, encrypted at rest, and after the socket closes uploads it through
//! `POST /sessions/{id}/me-audio` (a presigned S3 URL), deleting the local copy once the
//! upload succeeds. That endpoint comes with the Copilot API in build step 4, so for now the
//! pipeline calls a [`MeAudioSink`] that keeps nothing.
//!
//! To add recording: implement `MeAudioSink` to append the echo-cancelled 16 kHz samples to
//! an encrypted file, return its path from `finish`, and have the engine upload it after the
//! session's close and before deleting it (see `Engine::on_session_closed`).

use std::path::PathBuf;

pub trait MeAudioSink: Send {
    /// One tick of the user's echo-cancelled microphone audio, 16 kHz mono.
    fn on_audio(&mut self, capture_ms: u32, samples: &[f32]);
    /// The capture stopped; returns the recording to upload, if any.
    fn finish(&mut self) -> Option<PathBuf>;
}

/// Stage 0: nothing is recorded, so nothing is uploaded.
#[derive(Default)]
pub struct NoRecording;

impl MeAudioSink for NoRecording {
    fn on_audio(&mut self, _capture_ms: u32, _samples: &[f32]) {}

    fn finish(&mut self) -> Option<PathBuf> {
        None
    }
}
