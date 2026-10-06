//! Lines the two capture streams up on one 10 ms tick clock.
//!
//! Each stream arrives in device-sized packets on its own clock, and WASAPI loopback delivers
//! nothing at all while the system plays no sound. Ticks therefore run on the wall clock:
//! each tick takes 160 samples from each stream's FIFO and fills with silence where a stream
//! has none. A FIFO waits until it holds [`PRIME_MS`] before it plays (and again after
//! running dry), which absorbs packet jitter, and drops its oldest audio when it grows past
//! [`MAX_MS`], which bounds latency when a device clock runs fast. Echo cancellation needs
//! the far end and the mic in step; AEC3's delay estimator absorbs what the priming shifts.

use std::collections::VecDeque;

use super::TICK_SAMPLES;

pub const PRIME_MS: usize = 40;
pub const MAX_MS: usize = 300;
const PER_MS: usize = 16;

#[derive(Default)]
pub struct Fifo {
    buf: VecDeque<f32>,
    primed: bool,
    pub underruns: u64,
    pub dropped_samples: u64,
}

impl Fifo {
    pub fn push(&mut self, samples: &[f32]) {
        self.buf.extend(samples);
        let max = MAX_MS * PER_MS;
        if self.buf.len() > max {
            let excess = self.buf.len() - PRIME_MS * PER_MS;
            self.buf.drain(..excess);
            self.dropped_samples += excess as u64;
        }
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    /// Fill `out` with the next tick; silence (or a partial tick) when not enough has arrived.
    /// Returns true if any of it is real audio.
    pub fn take_tick(&mut self, out: &mut [f32]) -> bool {
        debug_assert_eq!(out.len(), TICK_SAMPLES);
        if !self.primed {
            if self.buf.len() >= PRIME_MS * PER_MS {
                self.primed = true;
            } else {
                out.fill(0.0);
                return false;
            }
        }
        let n = self.buf.len().min(TICK_SAMPLES);
        for (o, s) in out.iter_mut().zip(self.buf.drain(..n)) {
            *o = s;
        }
        out[n..].fill(0.0);
        if n < TICK_SAMPLES {
            self.primed = false;
            if n > 0 {
                self.underruns += 1;
            }
        }
        n > 0
    }
}

/// Which ticks are due: tick `n` covers [n * 10 ms, (n + 1) * 10 ms) after `start`, and is
/// processed once that span plus the priming delay has passed.
pub struct Ticker {
    next: u64,
}

impl Ticker {
    pub fn new() -> Self {
        Self { next: 0 }
    }

    pub fn next_index(&self) -> u64 {
        self.next
    }

    /// Ticks due at `elapsed_ms` since start; advances past them.
    pub fn due(&mut self, elapsed_ms: u64) -> std::ops::Range<u64> {
        let first = self.next;
        let upto = elapsed_ms / 10;
        if upto > self.next {
            self.next = upto;
        }
        first..self.next
    }

    /// Milliseconds since start at which the next tick is due.
    pub fn next_due_ms(&self) -> u64 {
        (self.next + 1) * 10
    }
}

impl Default for Ticker {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tick(f: &mut Fifo) -> (bool, Vec<f32>) {
        let mut out = vec![0.0; TICK_SAMPLES];
        let real = f.take_tick(&mut out);
        (real, out)
    }

    #[test]
    fn waits_until_primed_then_plays_in_order() {
        let mut f = Fifo::default();
        f.push(&vec![0.5; 320]); // 20 ms
        assert_eq!(tick(&mut f), (false, vec![0.0; TICK_SAMPLES]));
        f.push(&(0..480).map(|i| i as f32).collect::<Vec<_>>()); // now 50 ms
        let (real, out) = tick(&mut f);
        assert!(real);
        assert_eq!(out, vec![0.5; TICK_SAMPLES]);
        assert_eq!(f.len(), 640);
    }

    #[test]
    fn silence_when_a_stream_runs_dry_then_reprimes() {
        let mut f = Fifo::default();
        f.push(&vec![0.1; 700]);
        for _ in 0..4 {
            assert!(tick(&mut f).0);
        }
        // 60 samples left: a partial tick, then silence until primed again.
        let (real, out) = tick(&mut f);
        assert!(real);
        assert_eq!(&out[..60], &[0.1; 60]);
        assert_eq!(&out[60..], &[0.0; 100]);
        assert_eq!(f.underruns, 1);
        f.push(&vec![0.2; 320]);
        assert!(!tick(&mut f).0);
    }

    #[test]
    fn drops_the_oldest_audio_when_too_far_behind() {
        let mut f = Fifo::default();
        f.push(&vec![0.0; MAX_MS * PER_MS]);
        assert_eq!(f.len(), MAX_MS * PER_MS);
        f.push(&[1.0; 16]);
        assert_eq!(f.len(), PRIME_MS * PER_MS);
        assert_eq!(f.dropped_samples, (MAX_MS * PER_MS + 16 - PRIME_MS * PER_MS) as u64);
    }

    #[test]
    fn ticker_runs_on_elapsed_time() {
        let mut t = Ticker::new();
        assert_eq!(t.due(5), 0..0);
        assert_eq!(t.due(10), 0..1);
        assert_eq!(t.due(10), 1..1);
        assert_eq!(t.due(47), 1..4);
        assert_eq!(t.next_due_ms(), 50);
    }
}
