//! Voice activity detection with Silero VAD, run on the client so silence is never sent
//! (ARCHITECTURE.md, "Desktop client").
//!
//! [`Segmenter`] is the same causal state machine as `evals/src/copilot_evals/vad.py`, the
//! replay tool's client model, with the same defaults, so latency measured by the replay tool
//! carries over: speech starts when a 32 ms window scores at least `threshold` and has lasted
//! `min_speech_ms`; it ends once the score has stayed under `threshold - 0.15` for
//! `min_silence_ms`. The wait after the last voiced window is the VAD hangover, which counts
//! against first-word latency.

use ort::session::Session;
use ort::session::builder::GraphOptimizationLevel;
use ort::value::Tensor;

use super::SAMPLE_RATE;

/// Silero's window at 16 kHz: 512 samples, 32 ms.
pub const WINDOW: usize = 512;
/// Silero v5 and later expect the last 64 samples of the previous window in front of each one.
const CONTEXT: usize = 64;

const MODEL: &[u8] = include_bytes!("../../models/silero_vad.onnx");

/// Speech probability for consecutive 512-sample windows of one stream.
pub trait SpeechModel: Send {
    fn probability(&mut self, window: &[f32]) -> f32;
    fn reset(&mut self) {}
}

#[derive(Debug, thiserror::Error)]
#[error("Silero VAD: {0}")]
pub struct VadError(String);

impl From<ort::Error> for VadError {
    fn from(e: ort::Error) -> Self {
        Self(e.to_string())
    }
}

pub struct Silero {
    session: Session,
    state: Vec<f32>, // [2, 1, 128]
    context: Vec<f32>,
    input: Vec<f32>,
}

impl Silero {
    pub fn new() -> Result<Self, VadError> {
        let session = Session::builder()?
            .with_optimization_level(GraphOptimizationLevel::Level3)?
            .with_intra_threads(1)?
            .with_inter_threads(1)?
            .commit_from_memory(MODEL)?;
        Ok(Self {
            session,
            state: vec![0.0; 2 * 128],
            context: vec![0.0; CONTEXT],
            input: Vec::with_capacity(CONTEXT + WINDOW),
        })
    }

    fn run(&mut self, window: &[f32]) -> Result<f32, VadError> {
        self.input.clear();
        self.input.extend_from_slice(&self.context);
        self.input.extend_from_slice(window);
        let input = Tensor::from_array(([1usize, CONTEXT + WINDOW], self.input.clone()))?;
        let state = Tensor::from_array(([2usize, 1, 128], self.state.clone()))?;
        let sr = Tensor::from_array(([1usize], vec![i64::from(SAMPLE_RATE)]))?;
        let n = self.state.len();
        let (p, next_state) = {
            let outputs = self.session.run(ort::inputs![input, state, sr])?;
            let (_, prob) = outputs[0].try_extract_tensor::<f32>()?;
            let (_, state) = outputs[1].try_extract_tensor::<f32>()?;
            (prob.first().copied().unwrap_or(0.0), state[..n].to_vec())
        };
        self.state = next_state;
        self.context.copy_from_slice(&window[WINDOW - CONTEXT..]);
        Ok(p)
    }
}

impl SpeechModel for Silero {
    fn probability(&mut self, window: &[f32]) -> f32 {
        debug_assert_eq!(window.len(), WINDOW);
        self.run(window).unwrap_or_else(|e| {
            tracing::warn!("{e}");
            0.0
        })
    }

    fn reset(&mut self) {
        self.state.fill(0.0);
        self.context.fill(0.0);
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VadParams {
    pub threshold: f32,
    pub min_silence_ms: u32,
    pub min_speech_ms: u32,
}

impl Default for VadParams {
    /// The replay tool's defaults (evals/src/copilot_evals/vad.py).
    fn default() -> Self {
        Self { threshold: 0.5, min_silence_ms: 200, min_speech_ms: 120 }
    }
}

/// A speech boundary. Sample positions count from the start of the stream.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VadEvent {
    /// Speech started at `edge`; the client knows at `decided`.
    Start { edge: u64, decided: u64 },
    /// Speech ended at `edge` (just after the last voiced window); known at `decided`.
    End { edge: u64, decided: u64 },
}

pub struct Segmenter {
    params: VadParams,
    window_index: u64,
    start: Option<u64>,
    started: bool,
    silence_from: Option<u64>,
}

impl Segmenter {
    pub fn new(params: VadParams) -> Self {
        Self { params, window_index: 0, start: None, started: false, silence_from: None }
    }

    fn samples(&self, ms: u32) -> u64 {
        u64::from(ms) * u64::from(SAMPLE_RATE) / 1000
    }

    pub fn speaking(&self) -> bool {
        self.started
    }

    /// Feed the probability of the next window; returns at most two events.
    pub fn push(&mut self, p: f32) -> Vec<VadEvent> {
        let w_start = self.window_index * WINDOW as u64;
        let w_end = w_start + WINDOW as u64;
        self.window_index += 1;
        let neg = (self.params.threshold - 0.15).max(0.01);
        let mut events = Vec::new();
        let Some(start) = self.start else {
            if p >= self.params.threshold {
                self.start = Some(w_start);
                self.silence_from = None;
                self.check_start(w_start, w_end, &mut events);
            }
            return events;
        };
        if p >= self.params.threshold {
            self.silence_from = None;
        } else if p < neg {
            let from = *self.silence_from.get_or_insert(w_start);
            if w_end - from >= self.samples(self.params.min_silence_ms) {
                if self.started {
                    events.push(VadEvent::End { edge: from, decided: w_end });
                }
                // Not started: a blip shorter than min_speech_ms, dropped.
                self.start = None;
                self.started = false;
                self.silence_from = None;
                return events;
            }
        }
        self.check_start(start, w_end, &mut events);
        events
    }

    fn check_start(&mut self, start: u64, w_end: u64, events: &mut Vec<VadEvent>) {
        if self.started {
            return;
        }
        let voiced_until = self.silence_from.unwrap_or(w_end);
        if voiced_until - start >= self.samples(self.params.min_speech_ms) {
            self.started = true;
            events.push(VadEvent::Start { edge: start, decided: w_end });
        }
    }
}

/// One channel's VAD: buffers ticks into Silero windows and segments the scores.
pub struct ChannelVad {
    model: Box<dyn SpeechModel>,
    segmenter: Segmenter,
    pending: Vec<f32>,
    pub last_probability: f32,
}

impl ChannelVad {
    pub fn new(model: Box<dyn SpeechModel>, params: VadParams) -> Self {
        Self {
            model,
            segmenter: Segmenter::new(params),
            pending: Vec::with_capacity(2 * WINDOW),
            last_probability: 0.0,
        }
    }

    pub fn push(&mut self, samples: &[f32]) -> Vec<VadEvent> {
        self.pending.extend_from_slice(samples);
        let mut events = Vec::new();
        while self.pending.len() >= WINDOW {
            let p = self.model.probability(&self.pending[..WINDOW]);
            self.last_probability = p;
            events.extend(self.segmenter.push(p));
            self.pending.drain(..WINDOW);
        }
        events
    }

    pub fn speaking(&self) -> bool {
        self.segmenter.speaking()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const W: u64 = WINDOW as u64;

    fn run(probs: &[f32]) -> Vec<VadEvent> {
        let mut s = Segmenter::new(VadParams::default());
        probs.iter().flat_map(|&p| s.push(p)).collect()
    }

    #[test]
    fn speech_then_silence() {
        // 10 voiced windows (320 ms), then silence: the end is decided 200 ms later, rounded
        // up to whole windows (7 windows = 224 ms).
        let mut probs = vec![0.9; 10];
        probs.extend([0.05; 10]);
        let events = run(&probs);
        assert_eq!(
            events,
            vec![
                // min_speech 120 ms = 1920 samples: known at the end of the 4th window.
                VadEvent::Start { edge: 0, decided: 4 * W },
                VadEvent::End { edge: 10 * W, decided: 17 * W },
            ]
        );
    }

    #[test]
    fn blips_shorter_than_min_speech_are_dropped() {
        let mut probs = vec![0.05, 0.9, 0.9, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05];
        probs.extend([0.05; 5]);
        assert_eq!(run(&probs), vec![]);
    }

    #[test]
    fn short_pauses_do_not_end_speech() {
        // A 128 ms dip inside speech stays one segment.
        let mut probs = vec![0.9; 6];
        probs.extend([0.1; 4]);
        probs.extend([0.9; 6]);
        probs.extend([0.05; 8]);
        let events = run(&probs);
        assert_eq!(events.len(), 2);
        assert_eq!(events[1], VadEvent::End { edge: 16 * W, decided: 23 * W });
    }

    #[test]
    fn scores_between_thresholds_hold_the_state() {
        // 0.4 is under the start threshold but above the end threshold (0.35).
        let mut probs = vec![0.9; 5];
        probs.extend([0.4; 20]);
        let events = run(&probs);
        assert_eq!(events.len(), 1, "{events:?}");
        let mut idle = vec![0.4; 20];
        idle.extend([0.05; 3]);
        assert!(run(&idle).is_empty());
    }

    #[test]
    fn silero_loads_and_scores_silence_low() {
        let mut vad = Silero::new().expect("model loads");
        let silence = vec![0.0f32; WINDOW];
        for _ in 0..5 {
            assert!(vad.probability(&silence) < 0.1);
        }
    }
}
