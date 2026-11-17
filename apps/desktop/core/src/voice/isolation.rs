//! Keeping a guest's voice out of the call (ARCHITECTURE.md, "Guest seats": a guest is heard
//! by the host alone; nothing they say may reach the meeting, the other side, or either
//! transcript).
//!
//! "Them" is captured in WASAPI loopback of a whole output device (audio/capture.rs), so any
//! sound this app plays on that device would be captured and transcribed, and a loudspeaker
//! would put it into the host's microphone, and so into the meeting. The host hears guests only
//! on a device that passes all three rules, and voice is refused otherwise:
//!
//! 1. It is another device than the one "them" is captured from.
//! 2. Windows says it is headphones or a headset (its endpoint form factor), never speakers or
//!    anything Windows cannot name.
//! 3. A 1 kHz tone, played through the exact path guests' voice takes (the window's audio
//!    element routed to that device, src/voice.ts), is heard in neither the call's loopback
//!    nor the microphone (`listen_for_tone`, `tone_leaks`), every time voice is turned on.
//!    It also catches what the first two cannot: a device that loops back to another,
//!    headphones loud enough for the mic, or a browser that did not route the sound.

use serde::Serialize;

use crate::audio::SAMPLE_RATE;

/// What Windows says an audio endpoint is (`PKEY_AudioEndpoint_FormFactor`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FormFactor {
    Headphones,
    Headset,
    Speakers,
    Other,
    Unknown,
}

impl FormFactor {
    /// EndpointFormFactor (mmdeviceapi.h): 1 Speakers, 3 Headphones, 5 Headset.
    pub fn from_windows(value: u32) -> Self {
        match value {
            1 => Self::Speakers,
            3 => Self::Headphones,
            5 => Self::Headset,
            _ => Self::Other,
        }
    }

    pub fn is_private(self) -> bool {
        matches!(self, Self::Headphones | Self::Headset)
    }
}

/// Why voice is refused on a device, in words for the host.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum Refusal {
    /// No voice device is chosen, or it is not there.
    NoDevice,
    /// The device "them" is captured from: a guest would be transcribed and, through the
    /// meeting app's own capture of it, heard on the call.
    SameAsCall,
    /// Not headphones or a headset: a loudspeaker reaches the microphone.
    NotPrivate { form_factor: FormFactor },
    /// The test tone was heard in the call's capture or the microphone.
    ToneLeaked { loopback_ms: u32, mic_ms: u32 },
}

impl Refusal {
    pub fn message(&self) -> String {
        match self {
            Self::NoDevice => "Choose headphones for guests' voice first.".into(),
            Self::SameAsCall => "Guests' voice needs other headphones than the device the call plays on: \
                 on that one it would reach the call and the transcript."
                .into(),
            Self::NotPrivate { .. } => "Guests' voice plays on headphones or a headset only: from a speaker \
                 the microphone would pick it up."
                .into(),
            Self::ToneLeaked { .. } => {
                "The test tone on those headphones reached the call or the microphone, \
                 so guests' voice stays off. Turn them down or choose others."
                    .into()
            }
        }
    }
}

/// Rules 1 and 2. `voice` and `call` are cpal device ids (`wasapi:<endpoint id>`), already
/// resolved: the default device's own id, never "default".
pub fn check(voice: Option<&str>, call: Option<&str>, form_factor: FormFactor) -> Result<(), Refusal> {
    let Some(voice) = voice.filter(|v| !v.is_empty()) else { return Err(Refusal::NoDevice) };
    if call.is_some_and(|c| c.eq_ignore_ascii_case(voice)) {
        return Err(Refusal::SameAsCall);
    }
    if !form_factor.is_private() {
        return Err(Refusal::NotPrivate { form_factor });
    }
    Ok(())
}

/// The test tone's pitch: far from the 660 Hz of the audio check, so the two never mix up.
pub const TONE_HZ: f32 = 1_000.0;
/// Heard for this long, the tone leaked.
pub const LEAK_MS: u32 = 100;

/// Finds the test tone in 16 kHz audio, 10 ms at a time (the Goertzel algorithm): a block
/// counts when the tone's frequency holds most of the block's energy and the block is not
/// near silence, so speech or noise in the room do not count as the tone.
#[derive(Debug, Default)]
pub struct ToneDetector {
    pending: Vec<f32>,
    pub heard_ms: u32,
}

const BLOCK: usize = SAMPLE_RATE as usize / 100;

impl ToneDetector {
    pub fn push(&mut self, samples: &[f32]) {
        self.pending.extend_from_slice(samples);
        let whole = self.pending.len() / BLOCK * BLOCK;
        for block in self.pending[..whole].chunks(BLOCK) {
            if tone_in(block) {
                self.heard_ms += 10;
            }
        }
        self.pending.drain(..whole);
    }
}

fn tone_in(block: &[f32]) -> bool {
    let energy: f32 = block.iter().map(|s| s * s).sum();
    let rms = (energy / block.len() as f32).sqrt();
    if crate::audio::check::dbfs(rms) < -55.0 {
        return false;
    }
    let w = 2.0 * std::f32::consts::PI * TONE_HZ / SAMPLE_RATE as f32;
    let coeff = 2.0 * w.cos();
    let (mut s1, mut s2) = (0.0f32, 0.0f32);
    for &x in block {
        let s0 = x + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
    }
    let power = s1 * s1 + s2 * s2 - coeff * s1 * s2;
    // A pure tone's Goertzel power is about energy * N / 2.
    power / (energy * block.len() as f32 / 2.0) > 0.5
}

/// The tone test's verdict (rule 3).
pub fn tone_leaks(loopback_ms: u32, mic_ms: u32) -> Result<(), Refusal> {
    if loopback_ms >= LEAK_MS || mic_ms >= LEAK_MS {
        return Err(Refusal::ToneLeaked { loopback_ms, mic_ms });
    }
    Ok(())
}

/// Rule 3: capture as a call does (the call's output in loopback, and the microphone) for
/// `ms`, while the window plays the test tone on the voice device through the very path the
/// guests' voice takes (src/voice.ts), and say how long each heard it, in ms.
pub fn listen_for_tone(
    ms: u32,
    call_output: Option<&str>,
    mic: Option<&str>,
) -> Result<(u32, u32), crate::audio::source::CaptureError> {
    use std::sync::mpsc::sync_channel;
    use std::time::{Duration, Instant};

    use crate::audio::capture::DeviceSource;
    use crate::audio::source::{AudioSource, Packet};

    let (tx, rx) = sync_channel::<Packet>(512);
    let mut source = DeviceSource::new(mic.map(String::from), call_output.map(String::from));
    source.start(tx)?;
    let (mut them, mut me) = (ToneDetector::default(), ToneDetector::default());
    let deadline = Instant::now() + Duration::from_millis(u64::from(ms));
    while let Some(left) = deadline.checked_duration_since(Instant::now()) {
        match rx.recv_timeout(left) {
            Ok(Packet::Them(s)) => them.push(&s),
            Ok(Packet::Me(s)) => me.push(&s),
            Err(_) => break,
        }
    }
    source.stop();
    Ok((them.heard_ms, me.heard_ms))
}

/// What Windows says the output device is. Anything that cannot be read is `Unknown`, which is
/// refused.
#[cfg(windows)]
pub fn form_factor(device_id: &str) -> FormFactor {
    use windows::Win32::Media::Audio::{
        IMMDeviceEnumerator, MMDeviceEnumerator, PKEY_AudioEndpoint_FormFactor,
    };
    use windows::Win32::System::Com::{
        CLSCTX_ALL, COINIT_MULTITHREADED, CoCreateInstance, CoInitializeEx, STGM_READ,
    };
    use windows::core::HSTRING;

    let Some(endpoint) = device_id.strip_prefix("wasapi:") else { return FormFactor::Unknown };
    // SAFETY: plain COM calls on this thread; every result is checked, and nothing outlives
    // the function.
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        let Ok(enumerator) =
            CoCreateInstance::<_, IMMDeviceEnumerator>(&MMDeviceEnumerator, None, CLSCTX_ALL)
        else {
            return FormFactor::Unknown;
        };
        let Ok(device) = enumerator.GetDevice(&HSTRING::from(endpoint)) else { return FormFactor::Unknown };
        let Ok(store) = device.OpenPropertyStore(STGM_READ) else { return FormFactor::Unknown };
        let Ok(value) = store.GetValue(&PKEY_AudioEndpoint_FormFactor) else { return FormFactor::Unknown };
        match u32::try_from(&value) {
            Ok(v) => FormFactor::from_windows(v),
            Err(_) => FormFactor::Unknown,
        }
    }
}

#[cfg(not(windows))]
pub fn form_factor(_device_id: &str) -> FormFactor {
    FormFactor::Unknown
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEADPHONES: &str = "wasapi:{0.0.0.00000000}.{aaaa}";
    const SPEAKERS: &str = "wasapi:{0.0.0.00000000}.{bbbb}";

    #[test]
    fn voice_needs_its_own_private_device() {
        assert_eq!(check(None, Some(SPEAKERS), FormFactor::Headphones), Err(Refusal::NoDevice));
        assert_eq!(
            check(Some(HEADPHONES), Some(HEADPHONES), FormFactor::Headphones),
            Err(Refusal::SameAsCall)
        );
        assert_eq!(
            check(Some(HEADPHONES), Some(SPEAKERS), FormFactor::Speakers),
            Err(Refusal::NotPrivate { form_factor: FormFactor::Speakers })
        );
        assert_eq!(
            check(Some(HEADPHONES), Some(SPEAKERS), FormFactor::Unknown),
            Err(Refusal::NotPrivate { form_factor: FormFactor::Unknown })
        );
        assert_eq!(check(Some(HEADPHONES), Some(SPEAKERS), FormFactor::Headset), Ok(()));
        assert_eq!(check(Some(HEADPHONES), Some(SPEAKERS), FormFactor::Headphones), Ok(()));
    }

    #[test]
    fn windows_form_factors() {
        assert_eq!(FormFactor::from_windows(1), FormFactor::Speakers);
        assert_eq!(FormFactor::from_windows(3), FormFactor::Headphones);
        assert_eq!(FormFactor::from_windows(5), FormFactor::Headset);
        assert_eq!(FormFactor::from_windows(4), FormFactor::Other); // a microphone
    }

    fn sine(hz: f32, ms: u32, gain: f32) -> Vec<f32> {
        let n = (SAMPLE_RATE * ms / 1000) as usize;
        (0..n)
            .map(|i| gain * (2.0 * std::f32::consts::PI * hz * i as f32 / SAMPLE_RATE as f32).sin())
            .collect()
    }

    #[test]
    fn the_tone_is_found_and_other_sound_is_not() {
        let mut d = ToneDetector::default();
        d.push(&sine(TONE_HZ, 500, 0.05));
        assert!(d.heard_ms >= 450, "{}", d.heard_ms);
        // A voice-like mix, the audio check's own tone, and near silence are not the tone.
        let mut other = ToneDetector::default();
        let speechy: Vec<f32> =
            sine(220.0, 500, 0.2).iter().zip(sine(440.0, 500, 0.1)).map(|(a, b)| a + b).collect();
        other.push(&speechy);
        other.push(&sine(660.0, 500, 0.2));
        other.push(&sine(TONE_HZ, 500, 0.0005));
        assert_eq!(other.heard_ms, 0);
    }

    #[test]
    fn a_leak_refuses_voice() {
        assert_eq!(tone_leaks(0, 30), Ok(()));
        assert_eq!(tone_leaks(120, 0), Err(Refusal::ToneLeaked { loopback_ms: 120, mic_ms: 0 }));
        assert_eq!(tone_leaks(0, 100), Err(Refusal::ToneLeaked { loopback_ms: 0, mic_ms: 100 }));
    }
}
