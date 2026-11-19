//! Ventura-compatible capture. ScreenCaptureKit supplies system audio; AudioQueue opens
//! the selected microphone and plays the audio-check tone on the selected output UID.
//! The system mix includes this app, matching the previous loopback/tone behavior.

use super::SAMPLE_RATE;
use super::resample::Resampler;
use super::source::Packet;
use std::sync::mpsc::SyncSender;

struct Frames {
    tx: SyncSender<Packet>,
    microphone: bool,
    format: Option<(u32, u16, Resampler)>,
}

impl Frames {
    fn new(tx: SyncSender<Packet>, microphone: bool) -> Self {
        Self { tx, microphone, format: None }
    }

    fn push(&mut self, samples: &[f32], rate: u32, channels: u16) -> Result<(), &'static str> {
        if !(8_000..=192_000).contains(&rate)
            || !(1..=8).contains(&channels)
            || !samples.len().is_multiple_of(usize::from(channels))
            || samples.len() > rate as usize * usize::from(channels)
            || samples.iter().any(|sample| !sample.is_finite())
        {
            return Err("The audio device supplied an unsupported PCM format.");
        }
        if self
            .format
            .as_ref()
            .is_none_or(|(old_rate, old_channels, _)| *old_rate != rate || *old_channels != channels)
        {
            self.format = Some((rate, channels, Resampler::new(rate, channels, SAMPLE_RATE)));
        }
        let mut mono = Vec::new();
        self.format.as_mut().unwrap().2.process(samples, &mut mono);
        if !mono.is_empty() {
            let packet = if self.microphone { Packet::Me(mono) } else { Packet::Them(mono) };
            // A stalled consumer must never block a native audio callback.
            let _ = self.tx.try_send(packet);
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
mod native {
    use super::super::source::{AudioSource, CaptureError, Packet};
    use super::Frames;
    use serde::{Deserialize, Serialize};
    use std::ffi::{CStr, CString, c_char, c_void};
    use std::sync::mpsc::{SyncSender, sync_channel};
    use std::sync::{Arc, Mutex};
    use std::thread::JoinHandle;

    unsafe extern "C" {
        fn copilot_audio_devices(output: bool) -> *mut c_char;
        fn copilot_audio_free(value: *mut c_char);
        fn copilot_audio_start(
            microphone_uid: *const c_char,
            context: *mut c_void,
            samples: unsafe extern "C" fn(*mut c_void, bool, *const f32, usize, u32, u16),
            error: unsafe extern "C" fn(*mut c_void, *const c_char),
            failure: *mut *mut c_char,
        ) -> *mut c_void;
        fn copilot_audio_stop(handle: *mut c_void);
        fn copilot_audio_tone(
            uid: *const c_char,
            samples: *const f32,
            count: usize,
            rate: u32,
            failure: *mut *mut c_char,
        ) -> bool;
    }

    #[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
    pub struct DeviceInfo {
        /// CoreAudio's stable UID, using the same prefix as the previous CPAL backend.
        pub id: String,
        pub name: String,
        pub is_default: bool,
    }

    unsafe fn take_string(raw: *mut c_char) -> Option<String> {
        if raw.is_null() {
            return None;
        }
        // SAFETY: the bridge returns an owned, NUL-terminated allocation.
        let text = unsafe { CStr::from_ptr(raw) }.to_string_lossy().into_owned();
        unsafe { copilot_audio_free(raw) };
        Some(text)
    }

    fn devices(output: bool) -> Vec<DeviceInfo> {
        // SAFETY: the returned string is owned and released exactly once.
        let Some(json) = (unsafe { take_string(copilot_audio_devices(output)) }) else {
            return Vec::new();
        };
        serde_json::from_str(&json).unwrap_or_else(|error| {
            tracing::warn!("could not read Mac audio devices: {error}");
            Vec::new()
        })
    }

    pub fn input_devices() -> Vec<DeviceInfo> {
        devices(false)
    }
    pub fn output_devices() -> Vec<DeviceInfo> {
        devices(true)
    }

    fn find(id: Option<&str>, output: bool) -> Result<DeviceInfo, CaptureError> {
        let devices = devices(output);
        if let Some(id) = id.filter(|id| !id.is_empty()) {
            if let Some(device) = devices.iter().find(|device| device.id == id) {
                return Ok(device.clone());
            }
            tracing::warn!("audio device {id} not found; using the default");
        }
        devices.into_iter().find(|device| device.is_default).ok_or(CaptureError::NoDevice(if output {
            "output"
        } else {
            "microphone"
        }))
    }

    pub fn resolve(id: Option<&str>, output: bool) -> Option<String> {
        find(id, output).ok().map(|device| device.id)
    }

    fn uid(device: &DeviceInfo) -> Result<CString, CaptureError> {
        CString::new(device.id.strip_prefix("coreaudio:").unwrap_or(&device.id))
            .map_err(|_| CaptureError::Device("Invalid audio device ID.".into()))
    }

    struct Callbacks {
        microphone: Mutex<Frames>,
        system: Mutex<Frames>,
        errors: Arc<Mutex<Option<String>>>,
    }
    impl Callbacks {
        fn error(&self, error: String) {
            tracing::warn!("Mac audio: {error}");
            if let Ok(mut slot) = self.errors.lock() {
                *slot = Some(error);
            }
        }
    }

    unsafe extern "C" fn on_samples(
        context: *mut c_void,
        microphone: bool,
        samples: *const f32,
        count: usize,
        rate: u32,
        channels: u16,
    ) {
        if context.is_null() || samples.is_null() || count == 0 {
            return;
        }
        // The native bridge owns the callback lifetime; stop drains both queues before
        // NativeCapture releases this box. Bound the length before making a slice.
        let context = unsafe { &*context.cast::<Callbacks>() };
        if count > 192_000 * 8 {
            context.error("The audio device supplied an oversized buffer.".into());
            return;
        }
        let samples = unsafe { std::slice::from_raw_parts(samples, count) };
        let state = if microphone { &context.microphone } else { &context.system };
        if let Ok(mut state) = state.lock() {
            if let Err(error) = state.push(samples, rate, channels) {
                context.error(error.into());
            }
        }
    }

    unsafe extern "C" fn on_error(context: *mut c_void, error: *const c_char) {
        if context.is_null() || error.is_null() {
            return;
        }
        // SAFETY: the bridge passes a borrowed string valid for this callback.
        unsafe { &*context.cast::<Callbacks>() }
            .error(unsafe { CStr::from_ptr(error) }.to_string_lossy().into_owned());
    }

    struct NativeCapture {
        handle: *mut c_void,
        _callbacks: Box<Callbacks>,
    }
    impl NativeCapture {
        fn start(
            microphone: &DeviceInfo,
            tx: SyncSender<Packet>,
            errors: Arc<Mutex<Option<String>>>,
        ) -> Result<Self, CaptureError> {
            let uid = uid(microphone)?;
            let mut callbacks = Box::new(Callbacks {
                microphone: Mutex::new(Frames::new(tx.clone(), true)),
                system: Mutex::new(Frames::new(tx, false)),
                errors,
            });
            let mut failure = std::ptr::null_mut();
            // SAFETY: the box stays at a stable address until the native stop has drained
            // callbacks. A failed start also drains callbacks before returning NULL.
            let handle = unsafe {
                copilot_audio_start(
                    uid.as_ptr(),
                    (&mut *callbacks as *mut Callbacks).cast(),
                    on_samples,
                    on_error,
                    &mut failure,
                )
            };
            let failure = unsafe { take_string(failure) };
            if handle.is_null() {
                return Err(CaptureError::Device(
                    failure.unwrap_or_else(|| "Mac audio capture could not start.".into()),
                ));
            }
            Ok(Self { handle, _callbacks: callbacks })
        }
    }
    impl Drop for NativeCapture {
        fn drop(&mut self) {
            // SAFETY: this unique handle is stopped once, before the callbacks box is freed.
            unsafe { copilot_audio_stop(self.handle) };
        }
    }

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
            self.stop();
            let (mic, output, errors) = (self.mic_id.clone(), self.output_id.clone(), self.errors.clone());
            if let Ok(mut slot) = errors.lock() {
                *slot = None;
            }
            let (ready_tx, ready_rx) = sync_channel(1);
            let (stop_tx, stop_rx) = sync_channel(1);
            let handle = std::thread::Builder::new()
                .name("copilot-capture".into())
                .spawn(move || {
                    let capture = (|| {
                        // Output still identifies playback/tone routing. SCK captures the
                        // system mix rather than a single physical output's loopback.
                        find(output.as_deref(), true)?;
                        NativeCapture::start(&find(mic.as_deref(), false)?, tx, errors)
                    })();
                    match capture {
                        Ok(capture) => {
                            if ready_tx.send(Ok(())).is_ok() {
                                let _ = stop_rx.recv();
                            }
                            drop(capture);
                        }
                        Err(error) => {
                            let _ = ready_tx.send(Err(error));
                        }
                    }
                })
                .map_err(|error| CaptureError::Other(error.to_string()))?;
            match ready_rx.recv() {
                Ok(Ok(())) => {
                    self.handle = Some((handle, stop_tx));
                    Ok(())
                }
                result => {
                    let _ = handle.join();
                    match result {
                        Ok(Err(error)) => Err(error),
                        Err(error) => Err(CaptureError::Other(error.to_string())),
                        _ => unreachable!(),
                    }
                }
            }
        }
        fn stop(&mut self) {
            if let Some((handle, stop)) = self.handle.take() {
                let _ = stop.send(());
                let _ = handle.join();
            }
        }
    }
    impl Drop for DeviceSource {
        fn drop(&mut self) {
            self.stop();
        }
    }

    pub fn play_tone(output_id: Option<&str>) -> Result<(), CaptureError> {
        let device = uid(&find(output_id, true)?)?;
        let samples = super::super::check::tone(super::super::check::TONE_MS, super::super::SAMPLE_RATE);
        let mut failure = std::ptr::null_mut();
        // SAFETY: the blocking bridge copies these samples into its own output buffer.
        let played = unsafe {
            copilot_audio_tone(
                device.as_ptr(),
                samples.as_ptr(),
                samples.len(),
                super::super::SAMPLE_RATE,
                &mut failure,
            )
        };
        let failure = unsafe { take_string(failure) };
        if played {
            Ok(())
        } else {
            Err(CaptureError::Device(
                failure.unwrap_or_else(|| "Could not play the test tone on this output.".into()),
            ))
        }
    }
}
#[cfg(target_os = "macos")]
pub use native::*;

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::sync_channel;

    #[test]
    fn keeps_microphone_and_system_audio_separate_and_downmixes_stereo() {
        let (tx, rx) = sync_channel(4);
        let mut mic = Frames::new(tx.clone(), true);
        let mut system = Frames::new(tx, false);
        mic.push(&[0.25, -0.25], 16_000, 1).unwrap();
        system.push(&[0.2, 0.6, -0.2, -0.6], 16_000, 2).unwrap();
        assert!(matches!(rx.recv().unwrap(), Packet::Me(samples) if samples == [0.25, -0.25]));
        assert!(matches!(rx.recv().unwrap(), Packet::Them(samples) if samples == [0.4, -0.4]));
    }

    #[test]
    fn capture_handles_format_changes_and_rejects_corrupt_pcm() {
        let (tx, rx) = sync_channel(4);
        let mut capture = Frames::new(tx, false);
        capture.push(&vec![0.2; 4_800], 48_000, 1).unwrap();
        assert!(
            matches!(rx.recv().unwrap(), Packet::Them(samples) if samples.len() > 1_500 && samples.len() <= 1_600)
        );
        capture.push(&[0.1, 0.2], 16_000, 1).unwrap();
        assert!(matches!(rx.recv().unwrap(), Packet::Them(samples) if samples == [0.1, 0.2]));
        assert!(capture.push(&[1.0], 0, 1).is_err());
        assert!(capture.push(&[1.0], 16_000, 0).is_err());
        assert!(capture.push(&[1.0], 16_000, 2).is_err());
        assert!(capture.push(&[f32::NAN], 16_000, 1).is_err());
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn a_full_consumer_queue_drops_audio_without_blocking() {
        let (tx, rx) = sync_channel(1);
        let mut capture = Frames::new(tx, false);
        capture.push(&[0.1], 16_000, 1).unwrap();
        capture.push(&[0.2], 16_000, 1).unwrap();
        assert!(matches!(rx.recv().unwrap(), Packet::Them(samples) if samples == [0.1]));
        assert!(rx.try_recv().is_err());
    }
}
