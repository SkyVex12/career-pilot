//! Audio: capture, resampling, echo cancellation, voice activity detection and framing
//! (ARCHITECTURE.md, "Desktop client" and "Audio frames").
//!
//! Everything after capture runs on a 10 ms tick at 16 kHz mono: [`clock`] lines the two
//! capture streams up into ticks, [`pipeline::Pipeline`] runs AEC and VAD on each tick and
//! turns speech into [`OutFrame`]s, which the network client numbers and sends.

pub mod aec;
pub mod capture;
pub mod clock;
pub mod framer;
pub mod pipeline;
pub mod resample;
pub mod source;
pub mod vad;

/// The protocol's audio rate.
pub const SAMPLE_RATE: u32 = 16_000;
/// One tick: 10 ms, the unit WebRTC audio processing works in.
pub const TICK_SAMPLES: usize = 160;
pub const TICK_MS: u32 = 10;
/// A regular speech frame on the wire: 100 ms.
pub const FRAME_SAMPLES: usize = 1_600;

pub fn samples_to_ms(samples: u64) -> u32 {
    u32::try_from(samples * 1000 / u64::from(SAMPLE_RATE)).unwrap_or(u32::MAX)
}

/// A frame ready for the wire, before the client gives it a `frame_seq`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutFrame {
    pub channel: u8,
    pub flags: u8,
    /// Milliseconds since the pipeline started.
    pub capture_ms: u32,
    /// 16 kHz mono PCM. Always empty on the "me" channel.
    pub audio: Vec<i16>,
}

impl OutFrame {
    pub fn audio_bytes(&self) -> Vec<u8> {
        self.audio.iter().flat_map(|s| s.to_le_bytes()).collect()
    }
}

pub fn to_i16(samples: &[f32]) -> Vec<i16> {
    samples.iter().map(|s| (s.clamp(-1.0, 1.0) * 32767.0).round() as i16).collect()
}

pub fn to_f32(samples: &[i16]) -> Vec<f32> {
    samples.iter().map(|&s| f32::from(s) / 32768.0).collect()
}
