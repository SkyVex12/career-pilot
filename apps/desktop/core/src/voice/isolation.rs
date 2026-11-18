//! What kind of device an output is, as Windows says (ARCHITECTURE.md, "Guest seats",
//! Voice). The host may play guests' voice on any device; the picker only warns about one
//! Windows does not call headphones or a headset, since from a loudspeaker the microphone
//! would carry a guest into the meeting.

use serde::Serialize;

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

/// What Windows says the output device is. Anything that cannot be read is `Unknown`.
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

    #[test]
    fn windows_form_factors() {
        assert_eq!(FormFactor::from_windows(1), FormFactor::Speakers);
        assert_eq!(FormFactor::from_windows(3), FormFactor::Headphones);
        assert_eq!(FormFactor::from_windows(5), FormFactor::Headset);
        assert_eq!(FormFactor::from_windows(4), FormFactor::Other); // a microphone
    }
}
