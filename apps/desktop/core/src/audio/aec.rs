//! Acoustic echo cancellation on the microphone, with the system output as the reference
//! (ARCHITECTURE.md, "Desktop client"). Without headphones the speakers' sound reaches the
//! mic; cancelling it before VAD keeps the other side's voice from counting as the user's.
//!
//! [`WebRtcAec`] is WebRTC's AEC3 with its high-pass filter, from `sonora`, a pure Rust port of
//! WebRTC's audio processing module. The C++ module (`webrtc-audio-processing`) needs meson
//! and a C++ toolchain to build; the port needs neither and keeps the same algorithm. Both sit
//! behind [`EchoCanceller`], so swapping is local to this file.

use sonora::config::{EchoCanceller as Aec3, HighPassFilter};
use sonora::{AudioProcessing, Config, StreamConfig};

use super::{SAMPLE_RATE, TICK_SAMPLES};

pub trait EchoCanceller: Send {
    /// One 10 ms tick: `render` is what the speakers played, `capture` the mic, cleaned in place.
    fn process(&mut self, render: &[f32], capture: &mut [f32]);
}

/// For headphones, or to compare with and without cancellation.
pub struct NoEchoCancellation;

impl EchoCanceller for NoEchoCancellation {
    fn process(&mut self, _render: &[f32], _capture: &mut [f32]) {}
}

pub struct WebRtcAec {
    apm: AudioProcessing,
    render_out: Vec<f32>,
    capture_out: Vec<f32>,
}

impl WebRtcAec {
    pub fn new() -> Self {
        let config = Config {
            echo_canceller: Some(Aec3::default()),
            high_pass_filter: Some(HighPassFilter::default()),
            ..Default::default()
        };
        let stream = StreamConfig::new(SAMPLE_RATE, 1);
        let mut apm =
            AudioProcessing::builder().config(config).capture_config(stream).render_config(stream).build();
        // Capture and render are aligned on the same tick clock, so the only delay is the
        // device path (output latency plus the acoustic path); AEC3 estimates it from here.
        let _ = apm.set_stream_delay_ms(0);
        Self { apm, render_out: vec![0.0; TICK_SAMPLES], capture_out: vec![0.0; TICK_SAMPLES] }
    }
}

impl Default for WebRtcAec {
    fn default() -> Self {
        Self::new()
    }
}

impl EchoCanceller for WebRtcAec {
    fn process(&mut self, render: &[f32], capture: &mut [f32]) {
        debug_assert_eq!(render.len(), TICK_SAMPLES);
        debug_assert_eq!(capture.len(), TICK_SAMPLES);
        if self.apm.process_render_f32(&[render], &mut [&mut self.render_out]).is_err() {
            return;
        }
        let _ = self.apm.set_stream_delay_ms(0);
        if self.apm.process_capture_f32(&[capture], &mut [&mut self.capture_out]).is_ok() {
            capture.copy_from_slice(&self.capture_out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Speech-like noise: random bursts shaped to the voice band by a crude low-pass.
    fn voice_like(n: usize, seed: u64) -> Vec<f32> {
        let mut state = seed;
        let mut lp = 0.0f32;
        (0..n)
            .map(|i| {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                let white = ((state >> 33) as f32 / (1u64 << 31) as f32) - 0.5;
                lp = 0.7 * lp + 0.3 * white;
                let syllable = ((i as f32 / 16_000.0) * 4.0 * std::f32::consts::PI).sin().abs();
                lp * syllable * 0.8
            })
            .collect()
    }

    fn energy(x: &[f32]) -> f32 {
        x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32
    }

    #[test]
    fn cancels_a_delayed_attenuated_echo() {
        // The mic hears the far end 40 ms late through a two-tap "room", and nothing else.
        let secs = 8;
        let far = voice_like(16_000 * secs, 7);
        let delay = 640;
        let mic: Vec<f32> = (0..far.len())
            .map(|i| {
                let a = if i >= delay { far[i - delay] } else { 0.0 };
                let b = if i >= delay + 80 { far[i - delay - 80] } else { 0.0 };
                0.5 * a + 0.2 * b
            })
            .collect();
        let mut aec = WebRtcAec::new();
        let mut out = Vec::new();
        for (r, c) in far.chunks(TICK_SAMPLES).zip(mic.chunks(TICK_SAMPLES)) {
            let mut c = c.to_vec();
            aec.process(r, &mut c);
            out.extend(c);
        }
        // After AEC3 converges (give it 3 s), most echo energy is gone.
        let from = 16_000 * 3;
        let before = energy(&mic[from..]);
        let after = energy(&out[from..]);
        let reduction_db = 10.0 * (before / after.max(1e-12)).log10();
        assert!(reduction_db > 15.0, "echo reduced by only {reduction_db:.1} dB");
    }

    #[test]
    fn keeps_near_end_speech_when_the_far_end_is_silent() {
        let near = voice_like(16_000 * 4, 11);
        let silence = vec![0.0f32; TICK_SAMPLES];
        let mut aec = WebRtcAec::new();
        let mut out = Vec::new();
        for c in near.chunks(TICK_SAMPLES) {
            let mut c = c.to_vec();
            aec.process(&silence, &mut c);
            out.extend(c);
        }
        let from = 16_000;
        let kept = energy(&out[from..]) / energy(&near[from..]);
        assert!(kept > 0.5, "near-end speech lost: kept {kept:.2} of its energy");
    }
}
