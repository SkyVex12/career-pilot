//! The resend buffer (ARCHITECTURE.md, "Close codes, reconnect policy and limits"): while
//! disconnected, the client keeps the last 10 seconds of speech, both channels', and resends
//! it after the resume. The gateway drops frames whose `frame_seq` it already has, so
//! resending everything buffered is safe even when part of it arrived before the drop.
//!
//! The limit counts speech audio, not wall time, since only speech is sent. Flag-only frames
//! (speech ends, and the "me" channel when the gateway takes no mic audio) ride along in order,
//! so a speech start or end during the outage still reaches the gateway.

use std::collections::VecDeque;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RingFrame {
    pub channel: u8,
    pub frame_seq: u32,
    pub capture_ms: u32,
    /// The encoded frame, header included.
    pub bytes: Vec<u8>,
    pub audio_samples: usize,
}

pub struct FrameRing {
    frames: VecDeque<RingFrame>,
    audio_samples: usize,
    max_audio_samples: usize,
    max_frames: usize,
}

impl FrameRing {
    /// `seconds` of 16 kHz speech audio, either channel.
    pub fn new(seconds: u32) -> Self {
        Self {
            frames: VecDeque::new(),
            audio_samples: 0,
            max_audio_samples: seconds as usize * 16_000,
            // Flag-only frames are tiny but unbounded in a long silence; cap them too.
            max_frames: 4_096,
        }
    }

    pub fn push(&mut self, frame: RingFrame) {
        self.audio_samples += frame.audio_samples;
        self.frames.push_back(frame);
        while self.audio_samples > self.max_audio_samples || self.frames.len() > self.max_frames {
            let Some(old) = self.frames.pop_front() else { break };
            self.audio_samples -= old.audio_samples;
        }
    }

    /// Everything buffered, oldest first.
    pub fn iter(&self) -> impl Iterator<Item = &RingFrame> {
        self.frames.iter()
    }

    pub fn len(&self) -> usize {
        self.frames.len()
    }

    pub fn is_empty(&self) -> bool {
        self.frames.is_empty()
    }

    pub fn audio_ms(&self) -> usize {
        self.audio_samples / 16
    }

    pub fn clear(&mut self) {
        self.frames.clear();
        self.audio_samples = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use copilot_protocol::audio::{CHANNEL_ME, CHANNEL_THEM};

    fn them(seq: u32, samples: usize) -> RingFrame {
        RingFrame {
            channel: CHANNEL_THEM,
            frame_seq: seq,
            capture_ms: seq * 100,
            bytes: vec![0; 10 + samples * 2],
            audio_samples: samples,
        }
    }

    fn me(seq: u32) -> RingFrame {
        RingFrame {
            channel: CHANNEL_ME,
            frame_seq: seq,
            capture_ms: seq * 100,
            bytes: vec![1; 10],
            audio_samples: 0,
        }
    }

    #[test]
    fn keeps_the_last_ten_seconds_of_them_audio() {
        let mut ring = FrameRing::new(10);
        for seq in 0..150 {
            ring.push(them(seq, 1_600)); // 100 ms each, 15 s in all
        }
        assert_eq!(ring.audio_ms(), 10_000);
        let seqs: Vec<u32> = ring.iter().map(|f| f.frame_seq).collect();
        assert_eq!(seqs, (50..150).collect::<Vec<_>>());
    }

    #[test]
    fn the_users_speech_audio_counts_too() {
        let mut ring = FrameRing::new(1);
        ring.push(them(0, 8_000));
        ring.push(RingFrame { audio_samples: 8_000, bytes: vec![1; 16_010], ..me(0) });
        ring.push(them(1, 1_600)); // over 1 s of speech in all: the oldest frame goes
        let order: Vec<(u8, u32)> = ring.iter().map(|f| (f.channel, f.frame_seq)).collect();
        assert_eq!(order, vec![(CHANNEL_ME, 0), (CHANNEL_THEM, 1)]);
        assert_eq!(ring.audio_ms(), 600);
    }

    #[test]
    fn keeps_order_across_channels_with_flag_only_frames() {
        let mut ring = FrameRing::new(1);
        ring.push(them(0, 8_000));
        ring.push(me(0));
        ring.push(them(1, 8_000));
        ring.push(me(1));
        assert_eq!(ring.len(), 4);
        ring.push(them(2, 1_600)); // over 1 s: the oldest frame goes
        let order: Vec<(u8, u32)> = ring.iter().map(|f| (f.channel, f.frame_seq)).collect();
        assert_eq!(order, vec![(CHANNEL_ME, 0), (CHANNEL_THEM, 1), (CHANNEL_ME, 1), (CHANNEL_THEM, 2)]);
        assert_eq!(ring.audio_ms(), 600);
    }

    #[test]
    fn an_empty_speech_end_frame_is_kept_with_its_speech() {
        let mut ring = FrameRing::new(10);
        ring.push(them(0, 1_600));
        ring.push(them(1, 0)); // speech end, no audio
        assert_eq!(ring.len(), 2);
        assert_eq!(ring.audio_ms(), 100);
    }

    #[test]
    fn flag_frames_are_capped_too() {
        let mut ring = FrameRing::new(10);
        for seq in 0..5_000 {
            ring.push(me(seq));
        }
        assert_eq!(ring.len(), 4_096);
        assert_eq!(ring.iter().next().unwrap().frame_seq, 5_000 - 4_096);
    }

    #[test]
    fn clear_empties_it() {
        let mut ring = FrameRing::new(10);
        ring.push(them(0, 1_600));
        ring.clear();
        assert!(ring.is_empty());
        assert_eq!(ring.audio_ms(), 0);
    }
}
