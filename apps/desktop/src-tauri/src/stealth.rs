//! Stealth mode: keep every app window visible and usable on the monitor, and leave it out of
//! screen capture that honors window display affinity.
//!
//! On Windows 10 2004 and later this is `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
//! That flag omits the window. `WDA_MONITOR` (0x1) is never used: it paints a black rectangle
//! in the capture. If the call is rejected (older Windows, a window the flag does not cover,
//! or a capture path that ignores affinity), the window stays as it is and the failure is logged.

use tauri::{AppHandle, Manager, WebviewWindow};

/// `WDA_NONE`: capture includes the window again.
const WDA_NONE: u32 = 0x0000_0000;
/// `WDA_EXCLUDEFROMCAPTURE`: the window stays on the monitor and is left out of capture.
const WDA_EXCLUDEFROMCAPTURE: u32 = 0x0000_0011;

/// The affinity to pass to `SetWindowDisplayAffinity`. Exclusion is `0x11`, never `WDA_MONITOR`.
pub(crate) fn affinity(exclude: bool) -> u32 {
    if exclude { WDA_EXCLUDEFROMCAPTURE } else { WDA_NONE }
}

/// Apply the current choice to every window the app has open. One window failing does not
/// stop the others, and a failure does not hide or resize anything.
pub fn apply(app: &AppHandle, exclude: bool) {
    let affinity = affinity(exclude);
    for (label, window) in app.webview_windows() {
        set_window(&label, &window, affinity);
    }
}

#[cfg(windows)]
fn set_window(label: &str, window: &WebviewWindow, affinity: u32) {
    let hwnd = match window.hwnd() {
        Ok(hwnd) => hwnd,
        Err(e) => {
            tracing::warn!("stealth mode: no window handle for {label}: {e}");
            return;
        }
    };
    // Read the error before anything else can overwrite it.
    let failed =
        unsafe { if SetWindowDisplayAffinity(hwnd.0, affinity) == 0 { Some(GetLastError()) } else { None } };
    if let Some(err) = failed {
        tracing::warn!(
            "stealth mode: {label} could not change capture exclusion (SetWindowDisplayAffinity error {err}). \
             The window stays visible. This Windows version or this window may not support it, \
             and capture tools that ignore display affinity can still show the app."
        );
    }
}

#[cfg(windows)]
#[link(name = "user32")]
unsafe extern "system" {
    fn SetWindowDisplayAffinity(hwnd: *mut std::ffi::c_void, affinity: u32) -> i32;
}

// `GetLastError` lives in kernel32, not user32. Call it before any other Windows call.
#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetLastError() -> u32;
}

#[cfg(not(windows))]
fn set_window(label: &str, _window: &WebviewWindow, affinity: u32) {
    if affinity == WDA_EXCLUDEFROMCAPTURE {
        tracing::warn!(
            "stealth mode: {label} stays visible to capture; this system has no display-affinity exclusion"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::affinity;

    #[test]
    fn exclusion_omits_the_window_without_a_black_rectangle() {
        // WDA_MONITOR is 0x1 and draws a black rectangle where the window was. Do not use it.
        assert_eq!(affinity(true), 0x11);
        assert_eq!(affinity(false), 0);
        assert_ne!(affinity(true), 1);
    }
}
