//! The audio test, run from Settings or the Session tab before a call: a few seconds of the
//! chosen microphone and of the chosen output in loopback, while a short tone plays on that
//! output, judged from their levels.
//!
//! On 2026-10-29 one computer sent no microphone speech for two whole interviews, and nobody
//! knew until afterwards. The test says before the call whether the mic is heard and whether
//! the app hears what plays on the output the call uses.

use serde::Serialize;

use super::SAMPLE_RATE;
use super::source::Packet;

/// 10 ms blocks are judged one at a time.
const BLOCK: usize = SAMPLE_RATE as usize / 100;
/// A block this loud or louder is sound: a voice at normal distance is well above it.
pub const SOUND_DB: f32 = -45.0;
/// Below this peak the device delivers nothing usable: muted, unplugged, or the wrong one.
pub const SILENT_DB: f32 = -65.0;
/// How much sound from the mic counts as heard speech, and how much tone from the output.
pub const MIC_OK_MS: u32 = 600;
pub const CALL_OK_MS: u32 = 300;
/// The test tone: its length, pitch and level on the output.
pub const TONE_MS: u32 = 1_500;
pub const TONE_HZ: f32 = 660.0;
pub const TONE_GAIN: f32 = 0.2;

/// Decibels relative to full scale; -100 for silence.
pub fn dbfs(level: f32) -> f32 {
    if level <= 1e-5 { -100.0 } else { 20.0 * level.log10() }
}

#[derive(Debug, Default, Clone)]
struct Channel {
    pending: Vec<f32>,
    peak: f32,
    sound_ms: u32,
    heard_ms: u32,
    /// The last block's RMS, for the live meter.
    last_rms: f32,
}

impl Channel {
    fn push(&mut self, samples: &[f32]) {
        self.pending.extend_from_slice(samples);
        let whole = self.pending.len() / BLOCK * BLOCK;
        for block in self.pending[..whole].chunks(BLOCK) {
            let rms = (block.iter().map(|s| s * s).sum::<f32>() / BLOCK as f32).sqrt();
            let peak = block.iter().fold(0.0f32, |m, s| m.max(s.abs()));
            self.peak = self.peak.max(peak);
            self.last_rms = rms;
            self.heard_ms += 10;
            if dbfs(rms) >= SOUND_DB {
                self.sound_ms += 10;
            }
        }
        self.pending.drain(..whole);
    }
}

/// One side's result, as the screen shows it.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Verdict {
    pub ok: bool,
    pub message: String,
    /// The loudest sample, in dBFS.
    pub peak_db: f32,
    /// How long it was clearly above the noise floor.
    pub sound_ms: u32,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AudioCheck {
    pub mic: Verdict,
    pub call: Verdict,
}

/// The live meter, sent about ten times a second while the test runs.
#[derive(Debug, Clone, Copy, Serialize, PartialEq)]
pub struct Meter {
    pub mic_db: f32,
    pub call_db: f32,
}

/// Collects both channels for the length of the test.
#[derive(Debug, Default)]
pub struct Check {
    mic: Channel,
    call: Channel,
}

impl Check {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, packet: &Packet) {
        match packet {
            Packet::Me(s) => self.mic.push(s),
            Packet::Them(s) => self.call.push(s),
        }
    }

    pub fn meter(&self) -> Meter {
        Meter { mic_db: dbfs(self.mic.last_rms), call_db: dbfs(self.call.last_rms) }
    }

    /// The verdicts. `tone_played` is false when the tone could not be played, so silence on
    /// the output says nothing about the capture.
    pub fn result(&self, tone_played: bool) -> AudioCheck {
        AudioCheck { mic: mic_verdict(&self.mic), call: call_verdict(&self.call, tone_played) }
    }
}

fn mic_verdict(c: &Channel) -> Verdict {
    let peak_db = dbfs(c.peak);
    let (ok, message) = if c.heard_ms == 0 {
        (
            false,
            "No audio came from this microphone. Pick another one, or check that Windows lets apps use it.",
        )
    } else if peak_db < SILENT_DB {
        (false, "The microphone sends only silence: it may be muted, or it is not the one you speak into.")
    } else if c.sound_ms < MIC_OK_MS {
        (false, "Too quiet: speak during the test, move closer, or raise the microphone's level in Windows.")
    } else {
        (true, "Your voice comes through.")
    };
    Verdict { ok, message: message.into(), peak_db, sound_ms: c.sound_ms }
}

fn call_verdict(c: &Channel, tone_played: bool) -> Verdict {
    let peak_db = dbfs(c.peak);
    let (ok, message) = if c.sound_ms >= CALL_OK_MS {
        (true, "The app hears what plays on this output. Set the call app to play here too.")
    } else if !tone_played {
        (false, "The test sound could not be played on this output; play something on it and test again.")
    } else {
        (false, "Nothing was heard from this output. Pick the speakers or headset the call app plays on.")
    };
    Verdict { ok, message: message.into(), peak_db, sound_ms: c.sound_ms }
}

/// The test tone as 16 kHz samples, with 20 ms fades so it does not click.
pub fn tone(ms: u32, rate: u32) -> Vec<f32> {
    let n = (rate as usize * ms as usize) / 1000;
    let fade = (rate as usize / 50).max(1);
    (0..n)
        .map(|i| {
            let env = (i.min(n - 1 - i) as f32 / fade as f32).min(1.0);
            TONE_GAIN * env * (2.0 * std::f32::consts::PI * TONE_HZ * i as f32 / rate as f32).sin()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn noise(ms: u32, level: f32) -> Vec<f32> {
        let n = (SAMPLE_RATE * ms / 1000) as usize;
        (0..n).map(|i| if i % 2 == 0 { level } else { -level }).collect()
    }

    #[test]
    fn a_voice_and_the_tone_both_pass() {
        let mut c = Check::new();
        // Delivered in odd sizes, as devices do.
        for chunk in noise(1_000, 0.1).chunks(333) {
            c.push(&Packet::Me(chunk.to_vec()));
        }
        c.push(&Packet::Them(tone(TONE_MS, SAMPLE_RATE)));
        let r = c.result(true);
        assert!(r.mic.ok, "{:?}", r.mic);
        assert!(r.call.ok, "{:?}", r.call);
        assert!(r.mic.sound_ms >= 990);
        assert!(r.call.peak_db > -15.0);
    }

    #[test]
    fn a_muted_mic_and_a_silent_output_fail_with_the_reason() {
        let mut c = Check::new();
        c.push(&Packet::Me(noise(3_000, 0.0)));
        let r = c.result(true);
        assert!(!r.mic.ok);
        assert!(r.mic.message.contains("only silence"));
        assert!(!r.call.ok);
        assert!(r.call.message.contains("Nothing was heard"));
        assert!(Check::new().result(true).mic.message.starts_with("No audio came"));
        assert!(Check::new().result(false).call.message.contains("could not be played"));
    }

    #[test]
    fn a_quiet_mic_is_told_apart_from_a_dead_one() {
        let mut c = Check::new();
        c.push(&Packet::Me(noise(3_000, 0.002))); // about -54 dBFS: a far or turned-down mic
        let r = c.result(true);
        assert!(!r.mic.ok);
        assert!(r.mic.message.starts_with("Too quiet"), "{}", r.mic.message);
    }

    #[test]
    fn the_meter_follows_the_latest_block() {
        let mut c = Check::new();
        c.push(&Packet::Me(noise(100, 0.5)));
        assert!((c.meter().mic_db - dbfs(0.5)).abs() < 0.1);
        assert_eq!(c.meter().call_db, -100.0);
        let t = tone(100, SAMPLE_RATE);
        assert_eq!(t.len(), 1_600);
        assert!(t[0].abs() < 1e-6 && t.iter().fold(0.0f32, |m, s| m.max(s.abs())) <= TONE_GAIN);
    }
}
