//! Capture devices through cpal: the microphone ("me") and the system output ("them"). On
//! Windows, cpal opens an output device as an input in WASAPI loopback mode, so "them" is
//! exactly what the call app plays, whatever the call platform. (macOS needs
//! ScreenCaptureKit for this; that comes with the macOS build.)
//!
//! Each callback averages the device's channels and resamples to 16 kHz before handing the
//! audio on, so the audio thread only sees 16 kHz mono.

use std::sync::mpsc::{SyncSender, sync_channel};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat, Stream};
use serde::Serialize;

use super::SAMPLE_RATE;
use super::resample::Resampler;
use super::source::{AudioSource, CaptureError, Packet};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DeviceInfo {
    /// Stable id (cpal `DeviceId`), saved in the settings.
    pub id: String,
    pub name: String,
    pub is_default: bool,
}

fn info(device: &Device, default_id: Option<&str>) -> Option<DeviceInfo> {
    let id = device.id().ok()?.to_string();
    let name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| id.clone());
    Some(DeviceInfo { is_default: default_id == Some(id.as_str()), id, name })
}

/// Microphones.
pub fn input_devices() -> Vec<DeviceInfo> {
    let host = cpal::default_host();
    let default = host.default_input_device().and_then(|d| d.id().ok()).map(|i| i.to_string());
    host.input_devices()
        .map(|ds| ds.filter_map(|d| info(&d, default.as_deref())).collect())
        .unwrap_or_default()
}

/// Speakers and headsets; "them" is captured from one of these in loopback.
pub fn output_devices() -> Vec<DeviceInfo> {
    let host = cpal::default_host();
    let default = host.default_output_device().and_then(|d| d.id().ok()).map(|i| i.to_string());
    host.output_devices()
        .map(|ds| ds.filter_map(|d| info(&d, default.as_deref())).collect())
        .unwrap_or_default()
}

fn find(id: Option<&str>, output: bool) -> Result<Device, CaptureError> {
    let host = cpal::default_host();
    let what = if output { "output" } else { "microphone" };
    if let Some(id) = id.filter(|s| !s.is_empty()) {
        let parsed = id.parse().map_err(|e: cpal::Error| CaptureError::Device(e.to_string()))?;
        if let Some(d) = host.device_by_id(&parsed) {
            return Ok(d);
        }
        tracing::warn!("{what} device {id} not found; using the default");
    }
    let device = if output { host.default_output_device() } else { host.default_input_device() };
    device.ok_or(CaptureError::NoDevice(what))
}

fn open(
    device: &Device,
    loopback: bool,
    tx: SyncSender<Packet>,
    errors: Arc<Mutex<Option<String>>>,
) -> Result<Stream, CaptureError> {
    let dev_err = |e: cpal::Error| CaptureError::Device(e.to_string());
    let supported = if loopback { device.default_output_config() } else { device.default_input_config() }
        .map_err(dev_err)?;
    let config = supported.config();
    let mut resampler = Resampler::new(config.sample_rate, config.channels, SAMPLE_RATE);
    let wrap = move |mono: Vec<f32>| if loopback { Packet::Them(mono) } else { Packet::Me(mono) };
    let on_error = move |e: cpal::Error| {
        tracing::warn!("audio stream error: {e}");
        if let Ok(mut slot) = errors.lock() {
            *slot = Some(e.to_string());
        }
    };
    let mut out = Vec::with_capacity(4096);
    let mut deliver = move |samples: &[f32]| {
        out.clear();
        resampler.process(samples, &mut out);
        if !out.is_empty() {
            // A full queue means the audio thread is stalled; dropping beats blocking the device.
            let _ = tx.try_send(wrap(out.clone()));
        }
    };
    let stream = match supported.sample_format() {
        SampleFormat::F32 => {
            device.build_input_stream::<f32, _, _>(config, move |d, _| deliver(d), on_error, None)
        }
        SampleFormat::I16 => {
            let mut buf = Vec::new();
            device.build_input_stream::<i16, _, _>(
                config,
                move |d, _| {
                    buf.clear();
                    buf.extend(d.iter().map(|&s| f32::from(s) / 32768.0));
                    deliver(&buf);
                },
                on_error,
                None,
            )
        }
        SampleFormat::I32 => {
            let mut buf = Vec::new();
            device.build_input_stream::<i32, _, _>(
                config,
                move |d, _| {
                    buf.clear();
                    buf.extend(d.iter().map(|&s| s as f32 / 2_147_483_648.0));
                    deliver(&buf);
                },
                on_error,
                None,
            )
        }
        other => return Err(CaptureError::Device(format!("unsupported sample format {other:?}"))),
    }
    .map_err(dev_err)?;
    stream.play().map_err(dev_err)?;
    Ok(stream)
}

/// The two capture streams. cpal streams are not `Send` on every platform, so they live on a
/// thread of their own until `stop`.
pub struct DeviceSource {
    mic_id: Option<String>,
    output_id: Option<String>,
    handle: Option<(JoinHandle<()>, SyncSender<()>)>,
    pub errors: Arc<Mutex<Option<String>>>,
}

impl DeviceSource {
    pub fn new(mic_id: Option<String>, output_id: Option<String>) -> Self {
        Self { mic_id, output_id, handle: None, errors: Arc::new(Mutex::new(None)) }
    }
}

impl AudioSource for DeviceSource {
    fn start(&mut self, tx: SyncSender<Packet>) -> Result<(), CaptureError> {
        let (mic_id, output_id, errors) = (self.mic_id.clone(), self.output_id.clone(), self.errors.clone());
        let (ready_tx, ready_rx) = sync_channel::<Result<(), CaptureError>>(1);
        let (stop_tx, stop_rx) = sync_channel::<()>(1);
        let handle = std::thread::Builder::new()
            .name("copilot-capture".into())
            .spawn(move || {
                let streams = (|| {
                    let output = find(output_id.as_deref(), true)?;
                    let mic = find(mic_id.as_deref(), false)?;
                    let them = open(&output, true, tx.clone(), errors.clone())?;
                    let me = open(&mic, false, tx, errors)?;
                    Ok::<_, CaptureError>((them, me))
                })();
                match streams {
                    Ok(streams) => {
                        let _ = ready_tx.send(Ok(()));
                        let _ = stop_rx.recv();
                        drop(streams);
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                    }
                }
            })
            .map_err(|e| CaptureError::Other(e.to_string()))?;
        ready_rx.recv().map_err(|e| CaptureError::Other(e.to_string()))??;
        self.handle = Some((handle, stop_tx));
        Ok(())
    }

    fn stop(&mut self) {
        if let Some((handle, stop)) = self.handle.take() {
            let _ = stop.send(());
            let _ = handle.join();
        }
    }
}

/// Plays the audio test's tone ([`super::check::tone`]) once on an output device, on the
/// calling thread, and returns when it has played.
pub fn play_tone(output_id: Option<&str>) -> Result<(), CaptureError> {
    let device = find(output_id, true)?;
    let dev_err = |e: cpal::Error| CaptureError::Device(e.to_string());
    let supported = device.default_output_config().map_err(dev_err)?;
    let config = supported.config();
    let channels = usize::from(config.channels.max(1));
    let samples = super::check::tone(super::check::TONE_MS, config.sample_rate);
    let mut at = 0usize;
    let mut next = move || {
        let s = samples.get(at).copied().unwrap_or(0.0);
        at += 1;
        s
    };
    let on_error = |e: cpal::Error| tracing::warn!("test tone: {e}");
    let stream = match supported.sample_format() {
        SampleFormat::F32 => device.build_output_stream::<f32, _, _>(
            config,
            move |out, _| {
                for frame in out.chunks_mut(channels) {
                    frame.fill(next());
                }
            },
            on_error,
            None,
        ),
        SampleFormat::I16 => device.build_output_stream::<i16, _, _>(
            config,
            move |out, _| {
                for frame in out.chunks_mut(channels) {
                    #[allow(clippy::cast_possible_truncation)]
                    frame.fill((next() * 32767.0) as i16);
                }
            },
            on_error,
            None,
        ),
        other => return Err(CaptureError::Device(format!("unsupported sample format {other:?}"))),
    }
    .map_err(dev_err)?;
    stream.play().map_err(dev_err)?;
    std::thread::sleep(std::time::Duration::from_millis(u64::from(super::check::TONE_MS) + 150));
    drop(stream);
    Ok(())
}
