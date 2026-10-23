//! VAD events to wire frames (ARCHITECTURE.md, "Audio frames").
//!
//! Both channels send only speech. When speech starts, the 200 ms before it (the pre-roll) and
//! the audio up to the start decision go out at once with the start flag on the first frame,
//! in chunks of at most 1 s; then 100 ms frames in real time; when speech ends, the rest of
//! the audio and an empty frame with the end flag. This is `schedule()` in
//! evals/src/copilot_evals/replay.py, done live.
//!
//! "me" is the echo-cancelled mic, transcribed live by the gateway. When the gateway does not
//! ask for it (`session.ready` without `me_audio`), the network client strips the audio and
//! sends the flags alone (see `net::client`).

use std::collections::VecDeque;

#[cfg(test)]
use copilot_protocol::audio::{CHANNEL_ME, CHANNEL_THEM};
use copilot_protocol::audio::{FLAG_SPEECH_END, FLAG_SPEECH_START};

use super::vad::VadEvent;
use super::{FRAME_SAMPLES, OutFrame, SAMPLE_RATE, samples_to_ms};

pub const PREROLL_MS: u32 = 200;
const BURST_MAX: usize = SAMPLE_RATE as usize; // 1 s per frame keeps frames far under 64 KB
/// History kept for the pre-roll and the start decision (min_speech plus a window or two).
const HISTORY: usize = SAMPLE_RATE as usize * 2;

pub struct SpeechFramer {
    channel: u8,
    history: VecDeque<i16>,
    history_start: u64, // stream position of history[0]
    /// Next position to send while speaking.
    pos: Option<u64>,
    prev_end: u64,
    preroll: u64,
}

impl SpeechFramer {
    /// `channel` is the wire channel (`CHANNEL_THEM` or `CHANNEL_ME`).
    pub fn new(channel: u8) -> Self {
        Self {
            channel,
            history: VecDeque::with_capacity(HISTORY + FRAME_SAMPLES),
            history_start: 0,
            pos: None,
            prev_end: 0,
            preroll: u64::from(PREROLL_MS) * u64::from(SAMPLE_RATE) / 1000,
        }
    }

    fn end(&self) -> u64 {
        self.history_start + self.history.len() as u64
    }

    fn slice(&self, from: u64, to: u64) -> Vec<i16> {
        let a = (from - self.history_start) as usize;
        let b = (to - self.history_start) as usize;
        self.history.range(a..b).copied().collect()
    }

    fn frame(&self, flags: u8, at: u64, audio: Vec<i16>) -> OutFrame {
        OutFrame { channel: self.channel, flags, capture_ms: samples_to_ms(at), audio }
    }

    /// Append a tick of audio and the VAD events it produced; return the frames to send.
    pub fn push(&mut self, samples: &[i16], events: &[VadEvent]) -> Vec<OutFrame> {
        self.history.extend(samples);
        let mut out = Vec::new();
        for event in events {
            match *event {
                VadEvent::Start { edge, decided } => {
                    let decided = decided.min(self.end());
                    let mut from =
                        edge.saturating_sub(self.preroll).max(self.prev_end).max(self.history_start);
                    let mut first = true;
                    while from < decided {
                        let to = (from + BURST_MAX as u64).min(decided);
                        let flags = if first { FLAG_SPEECH_START } else { 0 };
                        out.push(self.frame(flags, from, self.slice(from, to)));
                        first = false;
                        from = to;
                    }
                    if first {
                        out.push(self.frame(FLAG_SPEECH_START, decided, Vec::new()));
                    }
                    self.pos = Some(decided);
                }
                VadEvent::End { decided, .. } => {
                    let Some(pos) = self.pos.take() else { continue };
                    let decided = decided.min(self.end());
                    if decided > pos {
                        out.push(self.frame(0, pos, self.slice(pos, decided)));
                    }
                    let at = decided.max(pos);
                    out.push(self.frame(FLAG_SPEECH_END, at, Vec::new()));
                    self.prev_end = at;
                }
            }
        }
        if let Some(mut pos) = self.pos {
            while pos + FRAME_SAMPLES as u64 <= self.end() {
                out.push(self.frame(0, pos, self.slice(pos, pos + FRAME_SAMPLES as u64)));
                pos += FRAME_SAMPLES as u64;
            }
            self.pos = Some(pos);
        }
        // Trim history, but never past audio still to be sent.
        let keep_from = self.end().saturating_sub(HISTORY as u64).min(self.pos.unwrap_or(u64::MAX));
        if keep_from > self.history_start {
            self.history.drain(..(keep_from - self.history_start) as usize);
            self.history_start = keep_from;
        }
        out
    }

    pub fn speaking(&self) -> bool {
        self.pos.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Samples whose value is their position, so a frame's audio shows where it came from.
    fn ramp(from: u64, n: usize) -> Vec<i16> {
        (0..n as u64).map(|i| ((from + i) % 30_000) as i16).collect()
    }

    fn feed(f: &mut SpeechFramer, total: usize, events: &[(u64, VadEvent)]) -> Vec<OutFrame> {
        let mut out = Vec::new();
        let mut pos = 0u64;
        while (pos as usize) < total {
            let tick = ramp(pos, 160);
            pos += 160;
            let due: Vec<VadEvent> =
                events.iter().filter(|(at, _)| *at > pos - 160 && *at <= pos).map(|(_, e)| *e).collect();
            out.extend(f.push(&tick, &due));
        }
        out
    }

    #[test]
    fn them_sends_preroll_speech_frames_and_an_empty_end() {
        let mut f = SpeechFramer::new(CHANNEL_THEM);
        // Speech from 1.0 s, known at 1.12 s; ends at 2.0 s, known at 2.2 s.
        let start = VadEvent::Start { edge: 16_000, decided: 17_920 };
        let end = VadEvent::End { edge: 32_000, decided: 35_200 };
        let frames = feed(&mut f, 48_000, &[(17_920, start), (35_200, end)]);

        let first = &frames[0];
        assert_eq!(first.flags, FLAG_SPEECH_START);
        assert_eq!(first.capture_ms, 800); // 200 ms of pre-roll
        assert_eq!(first.audio.len(), 17_920 - 12_800);
        assert_eq!(first.audio[0], 12_800);

        let last = frames.last().unwrap();
        assert_eq!(last.flags, FLAG_SPEECH_END);
        assert!(last.audio.is_empty());
        assert_eq!(last.capture_ms, 2_200);

        // The audio between is contiguous, every frame is at most 100 ms after the burst,
        // and nothing outside the speech (plus pre-roll and hangover) goes out.
        let audio: Vec<i16> = frames.iter().flat_map(|f| f.audio.clone()).collect();
        assert_eq!(audio, ramp(12_800, 35_200 - 12_800));
        assert!(frames[1..frames.len() - 1].iter().all(|f| f.flags == 0 && f.audio.len() <= FRAME_SAMPLES));
        assert!(frames.iter().filter(|f| f.flags & FLAG_SPEECH_START != 0).count() == 1);
    }

    #[test]
    fn regular_frames_follow_in_real_time() {
        let mut f = SpeechFramer::new(CHANNEL_THEM);
        let start = VadEvent::Start { edge: 0, decided: 1_920 };
        // Before the decision, nothing; at 1_920 the burst; then one frame per 1_600 samples.
        let mut sent = Vec::new();
        for i in 0..40u64 {
            let at = (i + 1) * 160;
            let ev: Vec<VadEvent> = if at == 1_920 { vec![start] } else { vec![] };
            let frames = f.push(&ramp(i * 160, 160), &ev);
            sent.push((at, frames));
        }
        assert!(sent.iter().take_while(|(at, _)| *at < 1_920).all(|(_, f)| f.is_empty()));
        let after: Vec<&OutFrame> = sent.iter().filter(|(at, _)| *at > 1_920).flat_map(|(_, f)| f).collect();
        assert!(after.iter().all(|f| f.audio.len() == FRAME_SAMPLES));
        assert_eq!(after.len(), (6_400 - 1_920) / FRAME_SAMPLES);
    }

    #[test]
    fn preroll_never_resends_the_previous_segment() {
        let mut f = SpeechFramer::new(CHANNEL_THEM);
        let events = [
            (3_200, VadEvent::Start { edge: 1_600, decided: 3_200 }),
            (8_000, VadEvent::End { edge: 4_800, decided: 8_000 }),
            // The next speech starts 50 ms after the last end decision.
            (11_200, VadEvent::Start { edge: 8_800, decided: 11_200 }),
        ];
        let frames = feed(&mut f, 12_800, &events);
        let starts: Vec<&OutFrame> = frames.iter().filter(|f| f.flags == FLAG_SPEECH_START).collect();
        assert_eq!(starts.len(), 2);
        assert_eq!(starts[1].capture_ms, 500); // from the previous end (8_000), not 8_800 - 3_200
        assert_eq!(starts[1].audio[0], 8_000);
    }

    #[test]
    fn me_sends_speech_audio_like_them() {
        let mut f = SpeechFramer::new(CHANNEL_ME);
        let start = VadEvent::Start { edge: 16_000, decided: 17_920 };
        let end = VadEvent::End { edge: 32_000, decided: 35_200 };
        let frames = feed(&mut f, 48_000, &[(17_920, start), (35_200, end)]);
        assert!(frames.iter().all(|f| f.channel == CHANNEL_ME));
        assert_eq!((frames[0].flags, frames[0].capture_ms), (FLAG_SPEECH_START, 800));
        let last = frames.last().unwrap();
        assert_eq!((last.flags, last.capture_ms), (FLAG_SPEECH_END, 2_200));
        // Speech only: nothing before the pre-roll or after the end decision.
        let audio: Vec<i16> = frames.iter().flat_map(|f| f.audio.clone()).collect();
        assert_eq!(audio, ramp(12_800, 35_200 - 12_800));
    }
}
