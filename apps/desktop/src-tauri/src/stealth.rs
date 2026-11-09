//! Stealth mode: keep every app window visible and usable on the monitor, and leave it out of
//! screen capture that honors window display affinity.
//!
//! On Windows 10 2004 and later this is `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
//! That flag omits the window. `WDA_MONITOR` (0x1) is never used: it paints a black rectangle
//! in the capture. If the call is rejected (older Windows, a window the flag does not cover,
//! or a capture path that ignores affinity), the window stays as it is and the failure is logged.
//!
//! While it is on, the main window also leaves the taskbar. It stays open and usable. The window
//! title becomes the configured display name, which is what Task Manager shows for that window
//! after a restart. The executable file name and the version-resource description (`productName`,
//! "Copilot Desktop") are written when the app is built and change only when a new build is installed.

use tauri::{AppHandle, Manager, WebviewWindow};

/// `WDA_NONE`: capture includes the window again.
const WDA_NONE: u32 = 0x0000_0000;
/// `WDA_EXCLUDEFROMCAPTURE`: the window stays on the monitor and is left out of capture.
const WDA_EXCLUDEFROMCAPTURE: u32 = 0x0000_0011;

/// The main window title in tauri.conf.json, restored when Stealth is off.
const MAIN_TITLE: &str = "Copilot";
/// The overlay builder title, restored when Stealth is off.
const OVERLAY_TITLE: &str = "Copilot overlay";
/// Shown in Task Manager for the window. Neutral, and the app's own name.
pub const DEFAULT_DISPLAY_NAME: &str = "Copilot";
const MAX_DISPLAY_NAME: usize = 64;

/// Windows' own process and Task Manager names. A display name must not use one of these.
const RESERVED: &[&str] = &[
    "applicationframehost",
    "conhost",
    "csrss",
    "ctfmon",
    "desktop window manager",
    "dllhost",
    "dwm",
    "explorer",
    "fontdrvhost",
    "lsass",
    "msmpeng",
    "registry",
    "runtimebroker",
    "searchhost",
    "securityhealthservice",
    "service host",
    "services",
    "shellexperiencehost",
    "sihost",
    "smss",
    "startmenuexperiencehost",
    "svchost",
    "system",
    "system idle process",
    "systemsettings",
    "taskhostw",
    "windows explorer",
    "windows installer",
    "wininit",
    "winlogon",
];

/// The Task Manager window name: this app's own short name, never a Windows component.
pub fn normalize_display_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    if name.is_empty() || name.chars().count() > MAX_DISPLAY_NAME {
        return Err("Enter a short name for this app, such as Copilot.".into());
    }
    if name
        .chars()
        .any(|c| c.is_control() || matches!(c, '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|'))
    {
        return Err("The name cannot contain a path or control character.".into());
    }
    let key = name.to_ascii_lowercase();
    let key = key.strip_suffix(".exe").unwrap_or(&key).trim().trim_end_matches('.');
    if RESERVED.contains(&key) {
        return Err("That name belongs to Windows. Use a plain name for this app, such as Copilot.".into());
    }
    Ok(name.to_string())
}

/// The affinity to pass to `SetWindowDisplayAffinity`. Exclusion is `0x11`, never `WDA_MONITOR`.
pub(crate) fn affinity(exclude: bool) -> u32 {
    if exclude { WDA_EXCLUDEFROMCAPTURE } else { WDA_NONE }
}

/// Apply capture exclusion, the taskbar button, and the window title. One window failing does
/// not stop the others, and a failure does not hide or close anything.
pub fn apply(app: &AppHandle, on: bool, display_name: &str) {
    let affinity = affinity(on);
    let name = if on { display_name } else { DEFAULT_DISPLAY_NAME };
    for (label, window) in app.webview_windows() {
        set_window(&label, &window, affinity);
        present(&label, &window, on, name);
    }
}

/// The taskbar button and the title Task Manager reads. The window stays open either way.
fn present(label: &str, window: &WebviewWindow, on: bool, display_name: &str) {
    match label {
        "main" => {
            if on {
                if matches!(window.is_minimized(), Ok(true)) {
                    let _ = window.unminimize();
                }
                // No taskbar button, and no minimize: a minimized window with no button is stuck.
                let _ = window.set_minimizable(false);
                let _ = window.set_skip_taskbar(true);
                let _ = window.set_title(display_name);
            } else {
                let _ = window.set_minimizable(true);
                let _ = window.set_skip_taskbar(false);
                let _ = window.set_title(MAIN_TITLE);
            }
        }
        "overlay" => {
            let _ = window.set_title(if on { display_name } else { OVERLAY_TITLE });
            // The overlay is normally off the taskbar. While Stealth is on it stays off even if
            // it was minimized onto a temporary button.
            if on || matches!(window.is_minimized(), Ok(false)) {
                let _ = window.set_skip_taskbar(true);
            }
        }
        _ => {
            if on {
                let _ = window.set_skip_taskbar(true);
                let _ = window.set_title(display_name);
            }
        }
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

    #[test]
    fn the_display_name_is_this_app_and_not_a_windows_component() {
        assert_eq!(super::normalize_display_name("  Copilot  ").unwrap(), "Copilot");
        assert_eq!(super::normalize_display_name("Copilot Desktop").unwrap(), "Copilot Desktop");
        assert!(super::normalize_display_name("  ").is_err());
        assert!(super::normalize_display_name("svchost.exe").is_err());
        assert!(super::normalize_display_name("Windows Explorer").is_err());
    }
}
