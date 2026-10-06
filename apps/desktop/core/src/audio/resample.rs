//! Streaming conversion of a capture device's stream to 16 kHz mono.
//!
//! Devices run at 44.1 or 48 kHz, often in stereo. Channels are averaged, then a windowed-sinc
//! low-pass at 0.45 of the output rate is evaluated at each output instant, from a table of
//! 256 filter phases. Downsampling only needs this one filter, and it keeps the stream
//! continuous across callbacks of any size.

use std::collections::VecDeque;
use std::f64::consts::PI;

const PHASES: usize = 256;
/// Filter half-width in output samples; the taps scale with the input/output ratio.
const HALF_WIDTH_OUT: f64 = 12.0;
const CUTOFF: f64 = 0.45; // of the output rate

pub struct Resampler {
    channels: usize,
    step: f64, // input samples per output sample
    half: usize,
    table: Vec<Vec<f32>>, // [phase][tap], 2 * half taps
    input: VecDeque<f32>,
    /// Position of the next output sample, in input samples relative to input[0].
    pos: f64,
    passthrough: bool,
}

impl Resampler {
    pub fn new(in_rate: u32, channels: u16, out_rate: u32) -> Self {
        let channels = usize::from(channels.max(1));
        let step = f64::from(in_rate) / f64::from(out_rate);
        let passthrough = in_rate == out_rate;
        // Below the output rate there is nothing to filter; above it, cut at CUTOFF of it.
        let scale = step.max(1.0);
        let half = (HALF_WIDTH_OUT * scale).ceil() as usize;
        let fc = CUTOFF / scale; // cycles per input sample
        let table = (0..PHASES)
            .map(|p| {
                let frac = p as f64 / PHASES as f64;
                let taps: Vec<f64> = (0..2 * half)
                    .map(|k| {
                        let t = k as f64 - (half as f64 - 1.0) - frac; // distance from the output instant
                        let x = 2.0 * fc * t;
                        let sinc = if x.abs() < 1e-9 { 1.0 } else { (PI * x).sin() / (PI * x) };
                        let w = t / (half as f64 + 1.0);
                        let blackman = 0.42 + 0.5 * (PI * w).cos() + 0.08 * (2.0 * PI * w).cos();
                        2.0 * fc * sinc * if w.abs() <= 1.0 { blackman } else { 0.0 }
                    })
                    .collect();
                let sum: f64 = taps.iter().sum();
                taps.iter().map(|t| (t / sum) as f32).collect()
            })
            .collect();
        Self { channels, step, half, table, input: VecDeque::new(), pos: 0.0, passthrough }
    }

    /// Push interleaved device samples; append 16 kHz mono output to `out`.
    pub fn process(&mut self, interleaved: &[f32], out: &mut Vec<f32>) {
        let ch = self.channels;
        let mono = interleaved.chunks_exact(ch).map(|f| f.iter().sum::<f32>() / ch as f32);
        if self.passthrough {
            out.extend(mono);
            return;
        }
        self.input.extend(mono);
        // Output instant n sits at input position pos; it needs taps from pos - (half - 1)
        // to pos + half.
        loop {
            let base = self.pos.floor();
            let first = base as isize - (self.half as isize - 1);
            let last = base as usize + self.half;
            if last >= self.input.len() {
                break;
            }
            let frac = self.pos - base;
            let phase = ((frac * PHASES as f64) as usize).min(PHASES - 1);
            let taps = &self.table[phase];
            let mut acc = 0.0f32;
            for (k, tap) in taps.iter().enumerate() {
                let i = first + k as isize;
                if i >= 0 {
                    acc += tap * self.input[i as usize];
                }
            }
            out.push(acc);
            self.pos += self.step;
        }
        // Drop input no future output needs.
        let keep_from = (self.pos.floor() as isize - self.half as isize).max(0) as usize;
        if keep_from > 0 {
            self.input.drain(..keep_from.min(self.input.len()));
            self.pos -= keep_from as f64;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(rate: u32, hz: f64, secs: f64, channels: usize) -> Vec<f32> {
        let n = (f64::from(rate) * secs) as usize;
        (0..n)
            .flat_map(|i| {
                let v = (2.0 * PI * hz * i as f64 / f64::from(rate)).sin() as f32 * 0.5;
                std::iter::repeat_n(v, channels)
            })
            .collect()
    }

    fn rms(x: &[f32]) -> f32 {
        (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt()
    }

    /// Frequency by zero crossings, over the steady middle of the signal.
    fn freq(x: &[f32], rate: f64) -> f64 {
        let mid = &x[x.len() / 4..x.len() * 3 / 4];
        let crossings = mid.windows(2).filter(|w| w[0] < 0.0 && w[1] >= 0.0).count();
        crossings as f64 * rate / mid.len() as f64
    }

    fn run(input: &[f32], in_rate: u32, channels: u16, chunk: usize) -> Vec<f32> {
        let mut r = Resampler::new(in_rate, channels, 16_000);
        let mut out = Vec::new();
        for c in input.chunks(chunk * usize::from(channels)) {
            r.process(c, &mut out);
        }
        out
    }

    #[test]
    fn keeps_a_voice_band_tone() {
        for (rate, ch) in [(48_000, 2), (44_100, 1), (32_000, 1)] {
            let out = run(&tone(rate, 1000.0, 1.0, ch), rate, ch as u16, 441);
            let expected = 16_000.0;
            assert!((out.len() as f64 - expected).abs() < 64.0, "{rate}: {} samples", out.len());
            assert!((freq(&out, 16_000.0) - 1000.0).abs() < 10.0, "{rate}");
            let level = rms(&out[out.len() / 4..out.len() * 3 / 4]);
            assert!((level - 0.5 / 2f32.sqrt()).abs() < 0.02, "{rate}: rms {level}");
        }
    }

    #[test]
    fn removes_what_would_alias() {
        // 12 kHz would fold back to 4 kHz at 16 kHz output.
        let out = run(&tone(48_000, 12_000.0, 1.0, 1), 48_000, 1, 480);
        assert!(rms(&out[out.len() / 4..]) < 0.005);
    }

    #[test]
    fn chunk_size_does_not_change_the_output() {
        let input = tone(48_000, 440.0, 0.5, 2);
        let a = run(&input, 48_000, 2, 480);
        let b = run(&input, 48_000, 2, 37);
        assert_eq!(a.len(), b.len());
        assert!(a.iter().zip(&b).all(|(x, y)| (x - y).abs() < 1e-6));
    }

    #[test]
    fn passes_16k_mono_through() {
        let input = tone(16_000, 300.0, 0.1, 1);
        assert_eq!(run(&input, 16_000, 1, 100), input);
    }
}
