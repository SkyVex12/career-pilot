//! Where audio comes from: the capture devices ([`super::capture::DeviceSource`]) or, for
//! tests and offline runs, [`FakeSource`], which plays prepared 16 kHz buffers in real time.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::SyncSender;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use super::TICK_SAMPLES;

/// 16 kHz mono audio from one side of the call.
#[derive(Debug)]
pub enum Packet {
    /// System output: the other side of the call.
    Them(Vec<f32>),
    /// Microphone: the user.
    Me(Vec<f32>),
}

#[derive(Debug, thiserror::Error)]
pub enum CaptureError {
    #[error("no {0} device found")]
    NoDevice(&'static str),
    #[error("audio device error: {0}")]
    Device(String),
    #[error("{0}")]
    Other(String),
}

pub trait AudioSource: Send {
    /// Start delivering packets; they may arrive in any size and from any thread.
    fn start(&mut self, tx: SyncSender<Packet>) -> Result<(), CaptureError>;
    fn stop(&mut self);
}

/// Plays two prepared buffers as "them" and "me", in 10 ms packets at real time, then
/// silence (or nothing, for "them", as WASAPI loopback does while nothing plays).
pub struct FakeSource {
    them: Arc<Vec<f32>>,
    me: Arc<Vec<f32>>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    pub loop_audio: bool,
}

impl FakeSource {
    pub fn new(them: Vec<f32>, me: Vec<f32>) -> Self {
        Self {
            them: Arc::new(them),
            me: Arc::new(me),
            stop: Arc::new(AtomicBool::new(false)),
            handle: None,
            loop_audio: false,
        }
    }

    /// The mic hears the speakers: "me" is the far end, `delay_ms` late and scaled by
    /// `gain`, plus `near` (the user's own voice). The echo case for the AEC test.
    pub fn with_echo(them: Vec<f32>, near: Vec<f32>, delay_ms: usize, gain: f32) -> Self {
        let delay = delay_ms * 16;
        let len = them.len().max(near.len());
        let me = (0..len)
            .map(|i| {
                let echo = if i >= delay { them.get(i - delay).copied().unwrap_or(0.0) * gain } else { 0.0 };
                (echo + near.get(i).copied().unwrap_or(0.0)).clamp(-1.0, 1.0)
            })
            .collect();
        Self::new(them, me)
    }
}

impl AudioSource for FakeSource {
    fn start(&mut self, tx: SyncSender<Packet>) -> Result<(), CaptureError> {
        let (them, me, stop, looping) =
            (self.them.clone(), self.me.clone(), self.stop.clone(), self.loop_audio);
        let handle = std::thread::Builder::new()
            .name("copilot-fake-audio".into())
            .spawn(move || {
                let start = Instant::now();
                let len = them.len().max(me.len());
                let mut i = 0usize;
                while !stop.load(Ordering::SeqCst) {
                    let pos = if looping && len > 0 { i % len } else { i };
                    let chunk = |b: &Vec<f32>| -> Option<Vec<f32>> {
                        (pos < b.len()).then(|| b[pos..(pos + TICK_SAMPLES).min(b.len())].to_vec())
                    };
                    if let Some(t) = chunk(&them) {
                        let _ = tx.try_send(Packet::Them(t));
                    }
                    let m = chunk(&me).unwrap_or_else(|| vec![0.0; TICK_SAMPLES]);
                    if tx.try_send(Packet::Me(m)).is_err() && stop.load(Ordering::SeqCst) {
                        break;
                    }
                    i += TICK_SAMPLES;
                    let due = start + Duration::from_micros((i as u64) * 1_000_000 / 16_000);
                    if let Some(wait) = due.checked_duration_since(Instant::now()) {
                        std::thread::sleep(wait);
                    }
                }
            })
            .map_err(|e| CaptureError::Other(e.to_string()))?;
        self.handle = Some(handle);
        Ok(())
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}
