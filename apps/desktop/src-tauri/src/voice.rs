//! Guest seats' voice in the app (ARCHITECTURE.md, "Guest seats", phase 2).
//!
//! The audio is WebRTC in the main window (LiveKit's JavaScript SDK; src/voice.ts). What
//! keeps it out of the call is here, in Rust, where the call's own devices are known:
//!
//! - `rules`: the chosen output device, found by the name the window shows for it, is not the
//!   device the call is captured from, and Windows says it is headphones or a headset;
//! - `listen`: while the window plays a test tone through the exact path guests' voice takes,
//!   capture as the call does and say whether the tone came through.
//!
//! The window turns voice on only when both pass.

use copilot_client::audio::capture;
use copilot_client::voice::isolation::{self, FormFactor, Refusal};
use serde::Serialize;

/// An output device as the voice picker shows it.
#[derive(Debug, Clone, Serialize)]
pub struct VoiceDevice {
    pub id: String,
    pub name: String,
    pub form_factor: FormFactor,
    /// Headphones or a headset (rule 2).
    pub private: bool,
    /// The device the call plays on and is captured from (rule 1): never for voice.
    pub call: bool,
}

pub fn devices(call_output: Option<&str>) -> Vec<VoiceDevice> {
    let call = capture::resolve(call_output, true);
    capture::output_devices()
        .into_iter()
        .map(|d| {
            let form_factor = isolation::form_factor(&d.id);
            let is_call = call.as_deref().is_some_and(|c| c.eq_ignore_ascii_case(&d.id));
            VoiceDevice {
                private: form_factor.is_private(),
                call: is_call,
                form_factor,
                id: d.id,
                name: d.name,
            }
        })
        .collect()
}

/// Rules 1 and 2 for the output device the window calls `name` (its label in WebView2 is
/// Windows' name for it). Exactly one device must have that name; anything else is refused.
pub fn rules(name: &str, call_output: Option<&str>) -> Result<(), Refusal> {
    let all = devices(call_output);
    let mut named = all.iter().filter(|d| d.name.eq_ignore_ascii_case(name.trim()));
    let (Some(device), None) = (named.next(), named.next()) else { return Err(Refusal::NoDevice) };
    let call = capture::resolve(call_output, true);
    isolation::check(Some(&device.id), call.as_deref(), device.form_factor)
}

/// Rule 3's listening half: the tone heard in the call's loopback and the microphone, in ms.
pub fn listen(ms: u32, call_output: Option<&str>, mic: Option<&str>) -> Result<(), Refusal> {
    match isolation::listen_for_tone(ms, call_output, mic) {
        Ok((loopback_ms, mic_ms)) => isolation::tone_leaks(loopback_ms, mic_ms),
        // The call's devices cannot be opened to listen: nothing proves the tone stayed out.
        Err(_) => Err(Refusal::NoDevice),
    }
}

/// WebView2 asks before a page may use the microphone. Guest seats' voice runs in the main
/// window, on the app's own pages only, so the app answers for the person there: the
/// microphone, for this app's own origin, and nothing else (every other request is left to
/// WebView2's own prompt). The window still opens the microphone only while a guest talks.
#[cfg(windows)]
pub fn allow_microphone(window: &tauri::WebviewWindow) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        COREWEBVIEW2_PERMISSION_KIND, COREWEBVIEW2_PERMISSION_KIND_MICROPHONE,
        COREWEBVIEW2_PERMISSION_STATE_ALLOW,
    };
    use webview2_com::{PermissionRequestedEventHandler, take_pwstr};

    let result = window.with_webview(|webview| {
        // SAFETY: COM calls on the webview's own thread (with_webview runs there); every
        // result is checked.
        unsafe {
            let Ok(core) = webview.controller().CoreWebView2() else { return };
            let handler = PermissionRequestedEventHandler::create(Box::new(|_, args| {
                let Some(args) = args else { return Ok(()) };
                let mut kind = COREWEBVIEW2_PERMISSION_KIND::default();
                args.PermissionKind(&mut kind)?;
                let mut uri = windows_core::PWSTR::null();
                args.Uri(&mut uri)?;
                if kind == COREWEBVIEW2_PERMISSION_KIND_MICROPHONE && own_page(&take_pwstr(uri)) {
                    args.SetState(COREWEBVIEW2_PERMISSION_STATE_ALLOW)?;
                }
                Ok(())
            }));
            let mut token = 0i64;
            if let Err(e) = core.add_PermissionRequested(&handler, &mut token) {
                tracing::warn!("voice: microphone permission handler not set: {e}");
            }
        }
    });
    if let Err(e) = result {
        tracing::warn!("voice: no webview for the microphone permission: {e}");
    }
}

#[cfg(not(windows))]
pub fn allow_microphone(_window: &tauri::WebviewWindow) {}

/// The app's own pages: the bundled ones, or the dev server in development.
fn own_page(uri: &str) -> bool {
    [
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    ]
    .iter()
    .any(|origin| uri == *origin || uri.starts_with(&format!("{origin}/")))
}

#[cfg(test)]
mod tests {
    use super::own_page;

    #[test]
    fn only_the_apps_own_pages_get_the_microphone() {
        assert!(own_page("http://tauri.localhost/"));
        assert!(own_page("http://tauri.localhost/index.html#/call"));
        assert!(own_page("http://127.0.0.1:1420/"));
        assert!(!own_page("http://tauri.localhost.evil.example/"));
        assert!(!own_page("https://example.com/"));
        assert!(!own_page(""));
    }
}
