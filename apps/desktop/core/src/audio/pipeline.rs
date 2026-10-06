//! Per-tick processing and the audio thread.
//!
//! [`Pipeline::process_tick`] takes one 10 ms tick of each stream: the system output goes to
//! the echo canceller as its reference and to the "them" VAD; the mic is cleaned by the echo
//! canceller, handed to the recording hook, and goes to the "me" VAD. The VAD events become
//! wire frames. [`AudioThread`] drives it on the wall clock from an [`AudioSource`].

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, sync_channel};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use copilot_protocol::Channel;

use super::aec::EchoCanceller;
use super::clock::{Fifo, PRIME_MS, Ticker};
use super::framer::{MeFramer, ThemFramer};
use super::source::{AudioSource, CaptureError, Packet};
use super::vad::{ChannelVad, VadEvent};
use super::{OutFrame, TICK_SAMPLES, samples_to_ms, to_i16};
use crate::recording::MeAudioSink;

/// What a tick produced, for the network client and the UI.
#[derive(Debug, Default)]
pub struct TickOutput {
    pub frames: Vec<OutFrame>,
    pub vad: Vec<VadChange>,
    /// When the audio thread processed the tick; None outside it.
    pub at: Option<Instant>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VadChange {
    pub channel: Channel,
    pub speaking: bool,
    /// Stream position (ms) where speech started or ended.
    pub edge_ms: u32,
    /// Stream position (ms) at which the VAD decided; the gap after `edge_ms` is the hangover.
    pub decided_ms: u32,
}

pub struct Pipeline {
    aec: Box<dyn EchoCanceller>,
    them_vad: ChannelVad,
    me_vad: ChannelVad,
    them_framer: ThemFramer,
    me_framer: MeFramer,
    recorder: Box<dyn MeAudioSink>,
    tick: u64,
    me_clean: Vec<f32>,
}

impl Pipeline {
    pub fn new(
        aec: Box<dyn EchoCanceller>,
        them_vad: ChannelVad,
        me_vad: ChannelVad,
        recorder: Box<dyn MeAudioSink>,
    ) -> Self {
        Self {
            aec,
            them_vad,
            me_vad,
            them_framer: ThemFramer::new(),
            me_framer: MeFramer,
            recorder,
            tick: 0,
            me_clean: vec![0.0; TICK_SAMPLES],
        }
    }

    pub fn capture_ms(&self) -> u32 {
        samples_to_ms(self.tick * TICK_SAMPLES as u64)
    }

    pub fn process_tick(&mut self, them: &[f32], me: &[f32]) -> TickOutput {
        let mut out = TickOutput::default();
        let at_ms = self.capture_ms();
        self.me_clean.copy_from_slice(me);
        self.aec.process(them, &mut self.me_clean);
        self.recorder.on_audio(at_ms, &self.me_clean);

        let them_events = self.them_vad.push(them);
        out.frames.extend(self.them_framer.push(&to_i16(them), &them_events));
        let me_events = self.me_vad.push(&self.me_clean);
        out.frames.extend(self.me_framer.push(&me_events));
        for (channel, events) in [(Channel::Them, &them_events), (Channel::Me, &me_events)] {
            out.vad.extend(events.iter().map(|e| match *e {
                VadEvent::Start { edge, decided } => VadChange {
                    channel,
                    speaking: true,
                    edge_ms: samples_to_ms(edge),
                    decided_ms: samples_to_ms(decided),
                },
                VadEvent::End { edge, decided } => VadChange {
                    channel,
                    speaking: false,
                    edge_ms: samples_to_ms(edge),
                    decided_ms: samples_to_ms(decided),
                },
            }));
        }
        self.tick += 1;
        out
    }

    pub fn finish(&mut self) {
        self.recorder.finish();
    }
}

/// Live levels for the settings and debug screens.
#[derive(Debug, Clone, Copy, Default, serde::Serialize)]
pub struct Levels {
    pub them_rms: f32,
    pub me_rms: f32,
    pub them_speech: f32,
    pub me_speech: f32,
    pub them_underruns: u64,
    pub me_underruns: u64,
}

/// Runs a [`Pipeline`] on its own thread at real time, fed by an [`AudioSource`].
pub struct AudioThread {
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    pub levels: Arc<Mutex<Levels>>,
}

impl AudioThread {
    /// `on_tick` runs on the audio thread for every tick with output.
    pub fn start(
        mut source: Box<dyn AudioSource>,
        mut pipeline: Pipeline,
        mut on_tick: impl FnMut(TickOutput) + Send + 'static,
    ) -> Result<Self, CaptureError> {
        let (tx, rx) = sync_channel::<Packet>(256);
        source.start(tx)?;
        let stop = Arc::new(AtomicBool::new(false));
        let levels = Arc::new(Mutex::new(Levels::default()));
        let started_at = Instant::now();
        let handle = {
            let stop = stop.clone();
            let levels = levels.clone();
            std::thread::Builder::new()
                .name("copilot-audio".into())
                .spawn(move || {
                    run(rx, &mut pipeline, &mut on_tick, &stop, &levels, started_at);
                    pipeline.finish();
                    source.stop();
                })
                .map_err(|e| CaptureError::Other(e.to_string()))?
        };
        Ok(Self { stop, handle: Some(handle), levels })
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

impl Drop for AudioThread {
    fn drop(&mut self) {
        self.stop();
    }
}

fn rms(x: &[f32]) -> f32 {
    (x.iter().map(|v| v * v).sum::<f32>() / x.len().max(1) as f32).sqrt()
}

fn run(
    rx: Receiver<Packet>,
    pipeline: &mut Pipeline,
    on_tick: &mut impl FnMut(TickOutput),
    stop: &AtomicBool,
    levels: &Mutex<Levels>,
    started_at: Instant,
) {
    let mut them = Fifo::default();
    let mut me = Fifo::default();
    let mut ticker = Ticker::new();
    let mut them_tick = vec![0.0f32; TICK_SAMPLES];
    let mut me_tick = vec![0.0f32; TICK_SAMPLES];
    let delay = Duration::from_millis(PRIME_MS as u64);
    let mut level_acc = (0.0f32, 0.0f32, 0u32);
    while !stop.load(Ordering::SeqCst) {
        let elapsed = started_at.elapsed().saturating_sub(delay).as_millis() as u64;
        let wait =
            Duration::from_millis(ticker.next_due_ms().saturating_sub(elapsed)).max(Duration::from_millis(1));
        match rx.recv_timeout(wait) {
            Ok(Packet::Them(s)) => them.push(&s),
            Ok(Packet::Me(s)) => me.push(&s),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        while let Ok(p) = rx.try_recv() {
            match p {
                Packet::Them(s) => them.push(&s),
                Packet::Me(s) => me.push(&s),
            }
        }
        let elapsed = started_at.elapsed().saturating_sub(delay).as_millis() as u64;
        for _ in ticker.due(elapsed) {
            them.take_tick(&mut them_tick);
            me.take_tick(&mut me_tick);
            let mut out = pipeline.process_tick(&them_tick, &me_tick);
            out.at = Some(Instant::now());
            level_acc.0 = level_acc.0.max(rms(&them_tick));
            level_acc.1 = level_acc.1.max(rms(&me_tick));
            level_acc.2 += 1;
            if level_acc.2 >= 5 {
                if let Ok(mut l) = levels.lock() {
                    l.them_rms = level_acc.0;
                    l.me_rms = level_acc.1;
                    l.them_speech = pipeline.them_vad.last_probability;
                    l.me_speech = pipeline.me_vad.last_probability;
                    l.them_underruns = them.underruns;
                    l.me_underruns = me.underruns;
                }
                level_acc = (0.0, 0.0, 0);
            }
            if !out.frames.is_empty() || !out.vad.is_empty() {
                on_tick(out);
            }
        }
    }
}
