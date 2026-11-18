//! Stealth mode: keep every app window visible and usable on the monitor, and leave it out of
//! screen capture that honors window display affinity.
//!
//! On Windows 10 2004 and later this is `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
//! That flag omits the window. `WDA_MONITOR` (0x1) is never used: it paints a black rectangle
//! in the capture. If the call is rejected (older Windows, a window the flag does not cover,
//! or a capture path that ignores affinity), the window stays as it is and the failure is logged.
//! Windows 11 21H2 (build 22000) rejects the flag with "not enough memory" while the window
//! holds WebView2's content, a child window that msedgewebview2.exe owns. There the WebView
//! moves to a hidden window while the flag is set, then moves back. That build also gets a
//! frame refresh after the flag, and a transparent overlay stays white there unless the frame
//! covers the whole client area.
//!
//! While it is on, the main window also leaves the taskbar. It stays open and usable. The main
//! window title is the configured display name. Task Manager's process name is the executable's
//! description, which `program_name` writes when that name is saved; it shows up after the next
//! start. The executable file name stays `copilot-desktop.exe`.
//!
//! The overlay is created hidden and transparent. Changing its title, taskbar tab, or capture
//! affinity while it is hidden makes Windows paint an opaque white rectangle where the overlay
//! sits, so those calls wait until the overlay is actually on screen.

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{AppHandle, Manager, WebviewWindow};

/// Stops a focus event raised by applying stealth from applying it again.
static APPLYING: AtomicBool = AtomicBool::new(false);

/// `WDA_NONE`: capture includes the window again.
const WDA_NONE: u32 = 0x0000_0000;
/// `WDA_EXCLUDEFROMCAPTURE`: the window stays on the monitor and is left out of capture.
const WDA_EXCLUDEFROMCAPTURE: u32 = 0x0000_0011;

/// Shown in Task Manager for this app. Neutral, and the app's own name.
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

/// Windows 11 21H2 drops capture exclusion until it is applied again. Later builds do not.
pub(crate) fn capture_needs_repeat() -> bool {
    #[cfg(windows)]
    {
        windows_build() < WINDOWS_11_22H2
    }
    #[cfg(not(windows))]
    {
        false
    }
}

/// Apply capture exclusion, the taskbar button, and the window title. One window failing does
/// not stop the others, and a failure does not hide or close anything.
pub fn apply(app: &AppHandle, on: bool, display_name: &str) {
    if APPLYING.swap(true, Ordering::AcqRel) {
        return;
    }
    let affinity = affinity(on);
    for (label, window) in app.webview_windows() {
        if label == "overlay" {
            // A hidden transparent overlay paints an opaque white rectangle if its title,
            // taskbar tab, or capture affinity changes. Leave it alone until it is shown.
            // Once it is on screen, its title has to match the main window or Task Manager
            // keeps a row under the old name.
            if window.is_visible().unwrap_or(false) {
                let _ = window.set_title(display_name);
                set_window(&label, &window, affinity);
                restore_overlay_transparency(&window);
            }
            continue;
        }
        set_window(&label, &window, affinity);
        present(&label, &window, on, display_name);
    }
    APPLYING.store(false, Ordering::Release);
}

/// Leave the overlay out of screen capture, or not, whatever the stealth setting: a guest
/// following someone's call keeps that private call out of their own screen shares
/// (ARCHITECTURE.md, "Guest seats"). Only while the overlay is on screen (see `apply`).
pub fn exclude_overlay(app: &AppHandle, exclude: bool) {
    let Some(window) = app.webview_windows().remove("overlay") else { return };
    if window.is_visible().unwrap_or(false) {
        set_window("overlay", &window, affinity(exclude));
        restore_overlay_transparency(&window);
    }
}

/// Every visible window's caption. The hidden overlay is left alone so it does not paint white.
/// Task Manager lists each visible window under the program name, so a window left as "Copilot"
/// stays on screen as its own row.
pub fn set_main_title(app: &AppHandle, display_name: &str) {
    for (label, window) in app.webview_windows() {
        if label == "overlay" && !window.is_visible().unwrap_or(false) {
            continue;
        }
        let _ = window.set_title(display_name);
        if label == "overlay" {
            restore_overlay_transparency(&window);
        }
    }
}

/// The taskbar button and the main window title. The window stays open either way.
/// The overlay is not handled here: touching it while it is hidden paints a white rectangle.
fn present(label: &str, window: &WebviewWindow, on: bool, display_name: &str) {
    if label != "main" {
        if on {
            let _ = window.set_skip_taskbar(true);
            let _ = window.set_title(display_name);
        }
        return;
    }
    let _ = window.set_title(display_name);
    if on {
        if matches!(window.is_minimized(), Ok(true)) {
            let _ = window.unminimize();
        }
        // No taskbar button, and no minimize: a minimized window with no button is stuck.
        let _ = window.set_minimizable(false);
        let _ = window.set_skip_taskbar(true);
    } else {
        let _ = window.set_minimizable(true);
        let _ = window.set_skip_taskbar(false);
    }
}

#[cfg(windows)]
fn set_window(label: &str, window: &WebviewWindow, affinity: u32) {
    let hwnd = match window.hwnd() {
        Ok(hwnd) => hwnd.0 as usize,
        Err(e) => {
            tracing::warn!("stealth mode: no window handle for {label}: {e}");
            return;
        }
    };
    // Settings and overlay commands run on worker threads. `with_webview` runs on the thread
    // that owns the window, and hands over the WebView2 controller that build 22000 needs.
    let label = label.to_string();
    let queued = window.with_webview(move |webview| {
        set_window_now(&label, hwnd as *mut std::ffi::c_void, affinity, &webview.controller())
    });
    if let Err(e) = queued {
        tracing::warn!("stealth mode: could not reach the window thread: {e}");
    }
}

#[cfg(windows)]
fn set_window_now(
    label: &str,
    hwnd: *mut std::ffi::c_void,
    affinity: u32,
    controller: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Controller,
) {
    let build = windows_build();
    // Already as asked: nothing to do. The focus repeat on 21H2 then costs nothing, and the
    // WebView is not moved again on every focus.
    if current_affinity(hwnd) == Some(affinity) {
        return;
    }
    // Read the error before anything else can overwrite it.
    let mut failed = set_affinity(hwnd, affinity);
    // Build 22000 answers ERROR_NOT_ENOUGH_MEMORY (8) for a window that has a child window
    // from another process (desktop.log, 2026-11-18: every call failed, at start and later,
    // with pointer input off). WebView2's content is such a child: msedgewebview2.exe owns
    // it. Move the WebView to a hidden window, set the flag, and move it back.
    if failed == Some(ERROR_NOT_ENOUGH_MEMORY) && build < WINDOWS_11_22H2 {
        log_foreign_children(label, hwnd);
        failed = set_with_webview_parked(hwnd, affinity, controller);
    }
    if let Some(err) = failed {
        tracing::warn!(
            "stealth mode: {label} could not change capture exclusion on build {build} \
             (SetWindowDisplayAffinity error {err}). The window stays visible to capture."
        );
        return;
    }
    // Build 22000 applies the affinity only after DWM rebuilds the frame, and that rebuild
    // can clear it. Set the flag again afterwards. Later builds already pick it up above.
    if build < WINDOWS_11_22H2 {
        refresh_frame(hwnd);
        if let Some(err) = set_affinity(hwnd, affinity) {
            tracing::warn!(
                "stealth mode: {label} could not keep capture exclusion after the frame refresh (error {err})"
            );
        }
    }
    // Read back what Windows kept. A call that succeeds but does not stick shows no error
    // code, so the log says which it was.
    match current_affinity(hwnd) {
        Some(kept) if kept == affinity => {
            tracing::info!("stealth mode: {label} capture affinity {kept:#x} on build {build}")
        }
        Some(kept) => tracing::warn!(
            "stealth mode: {label} asked for affinity {affinity:#x} on build {build}; Windows kept {kept:#x}"
        ),
        None => tracing::warn!("stealth mode: {label} affinity could not be read back on build {build}"),
    }
}

#[cfg(windows)]
fn current_affinity(hwnd: *mut std::ffi::c_void) -> Option<u32> {
    let mut kept = 0u32;
    (unsafe { GetWindowDisplayAffinity(hwnd, &mut kept) } != 0).then_some(kept)
}

/// Set the affinity while the WebView's window sits under a hidden window of this process,
/// then put the WebView back. `SetParentWindow` is WebView2's own way to move its content to
/// another parent. The content is hidden from the screen for a moment while it is moved.
/// Returns the affinity error, as `set_affinity` does.
#[cfg(windows)]
fn set_with_webview_parked(
    hwnd: *mut std::ffi::c_void,
    affinity: u32,
    controller: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Controller,
) -> Option<u32> {
    use windows::Win32::Foundation::HWND;
    const WS_POPUP: u32 = 0x8000_0000;
    const WS_EX_TOOLWINDOW: u32 = 0x0000_0080;
    let class: Vec<u16> = "STATIC\0".encode_utf16().collect();
    // A hidden window with no taskbar button. STATIC is a system class: nothing to register.
    let park = unsafe {
        CreateWindowExW(
            WS_EX_TOOLWINDOW,
            class.as_ptr(),
            std::ptr::null(),
            WS_POPUP,
            0,
            0,
            0,
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if park.is_null() {
        let err = unsafe { GetLastError() };
        tracing::warn!("stealth mode: no hidden window to hold the WebView (error {err})");
        return Some(ERROR_NOT_ENOUGH_MEMORY);
    }
    let result = match unsafe { controller.SetParentWindow(HWND(park)) } {
        Ok(()) => {
            let result = set_affinity(hwnd, affinity);
            if result.is_some() {
                // Still refused with the WebView moved out: the log says what is left.
                log_foreign_children("with the WebView moved out, the window", hwnd);
            }
            if let Err(e) = unsafe { controller.SetParentWindow(HWND(hwnd)) } {
                tracing::warn!("stealth mode: the WebView could not move back to its window: {e}");
            }
            let _ = unsafe { controller.NotifyParentWindowPositionChanged() };
            result
        }
        Err(e) => {
            tracing::warn!("stealth mode: the WebView could not move to a hidden window: {e}");
            Some(ERROR_NOT_ENOUGH_MEMORY)
        }
    };
    unsafe { DestroyWindow(park) };
    result
}

/// Log the child windows that another process owns, by class: the evidence for why build
/// 22000 refuses the affinity.
#[cfg(windows)]
fn log_foreign_children(label: &str, hwnd: *mut std::ffi::c_void) {
    unsafe extern "system" fn visit(child: *mut std::ffi::c_void, found: isize) -> i32 {
        let found = unsafe { &mut *(found as *mut Vec<String>) };
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(child, &mut pid) };
        if pid != unsafe { GetCurrentProcessId() } {
            let mut name = [0u16; 128];
            let len = unsafe { GetClassNameW(child, name.as_mut_ptr(), name.len() as i32) };
            found.push(String::from_utf16_lossy(&name[..len.max(0) as usize]));
        }
        1
    }
    let mut found: Vec<String> = Vec::new();
    unsafe { EnumChildWindows(hwnd, Some(visit), &mut found as *mut Vec<String> as isize) };
    tracing::info!(
        "stealth mode: {label} has {} child window(s) from another process: {}",
        found.len(),
        found.join(", ")
    );
}

/// The Windows build number for the log. 0 when it cannot be read, or off Windows.
pub fn build_number() -> u32 {
    #[cfg(windows)]
    {
        windows_build()
    }
    #[cfg(not(windows))]
    {
        0
    }
}

#[cfg(windows)]
fn set_affinity(hwnd: *mut std::ffi::c_void, affinity: u32) -> Option<u32> {
    unsafe { if SetWindowDisplayAffinity(hwnd, affinity) == 0 { Some(GetLastError()) } else { None } }
}

/// Capture affinity clears the blur region tao used to make the overlay transparent. Put that
/// empty region back so a visible overlay does not turn into a solid white panel.
#[cfg(windows)]
fn restore_overlay_transparency(window: &WebviewWindow) {
    let Ok(hwnd) = window.hwnd() else { return };
    unsafe {
        let region = CreateRectRgn(0, 0, -1, -1);
        if region.is_null() {
            return;
        }
        let blur =
            DwmBlurBehind { flags: DWM_BB_ENABLE | DWM_BB_BLURREGION, enable: 1, region, transition: 0 };
        let _ = DwmEnableBlurBehindWindow(hwnd.0, &blur);
        let _ = DeleteObject(region);
        // Build 22000 paints the client area opaque white unless the glass frame covers it.
        // Later Windows already stays transparent from the blur region alone.
        if windows_build() < WINDOWS_11_22H2 {
            let margins = DwmMargins { left: -1, right: -1, top: -1, bottom: -1 };
            let _ = DwmExtendFrameIntoClientArea(hwnd.0, &margins);
        }
    }
}

/// Ask DWM to rebuild the frame so display affinity takes effect on Windows 11 21H2.
#[cfg(windows)]
fn refresh_frame(hwnd: *mut std::ffi::c_void) {
    const SWP_NOSIZE: u32 = 0x0001;
    const SWP_NOMOVE: u32 = 0x0002;
    const SWP_NOZORDER: u32 = 0x0004;
    const SWP_NOACTIVATE: u32 = 0x0010;
    const SWP_FRAMECHANGED: u32 = 0x0020;
    unsafe {
        SetWindowPos(
            hwnd,
            std::ptr::null_mut(),
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        );
    }
}

#[cfg(windows)]
const WINDOWS_11_22H2: u32 = 22621;
#[cfg(windows)]
const ERROR_NOT_ENOUGH_MEMORY: u32 = 8;

#[cfg(windows)]
fn windows_build() -> u32 {
    static BUILD: std::sync::OnceLock<u32> = std::sync::OnceLock::new();
    *BUILD.get_or_init(|| {
        let mut info = OsVersionInfo {
            size: std::mem::size_of::<OsVersionInfo>() as u32,
            major: 0,
            minor: 0,
            build: 0,
            platform: 0,
            csd: [0; 128],
        };
        if unsafe { RtlGetVersion(&mut info) } == 0 { info.build } else { 0 }
    })
}

#[cfg(windows)]
#[repr(C)]
struct OsVersionInfo {
    size: u32,
    major: u32,
    minor: u32,
    build: u32,
    platform: u32,
    csd: [u16; 128],
}

#[cfg(windows)]
#[repr(C)]
struct DwmMargins {
    left: i32,
    right: i32,
    top: i32,
    bottom: i32,
}

#[cfg(not(windows))]
fn restore_overlay_transparency(_window: &WebviewWindow) {}

/// `DWM_BLURBEHIND`. Field order matches the Windows struct, including pointer alignment.
#[cfg(windows)]
#[repr(C)]
struct DwmBlurBehind {
    flags: u32,
    enable: i32,
    region: *mut std::ffi::c_void,
    transition: i32,
}

#[cfg(windows)]
const DWM_BB_ENABLE: u32 = 0x1;
#[cfg(windows)]
const DWM_BB_BLURREGION: u32 = 0x2;

#[cfg(windows)]
#[link(name = "user32")]
unsafe extern "system" {
    fn SetWindowDisplayAffinity(hwnd: *mut std::ffi::c_void, affinity: u32) -> i32;
    fn GetWindowDisplayAffinity(hwnd: *mut std::ffi::c_void, affinity: *mut u32) -> i32;
    fn CreateWindowExW(
        ex_style: u32,
        class: *const u16,
        name: *const u16,
        style: u32,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
        parent: *mut std::ffi::c_void,
        menu: *mut std::ffi::c_void,
        instance: *mut std::ffi::c_void,
        param: *mut std::ffi::c_void,
    ) -> *mut std::ffi::c_void;
    fn DestroyWindow(hwnd: *mut std::ffi::c_void) -> i32;
    fn EnumChildWindows(
        parent: *mut std::ffi::c_void,
        visit: Option<unsafe extern "system" fn(*mut std::ffi::c_void, isize) -> i32>,
        param: isize,
    ) -> i32;
    fn GetWindowThreadProcessId(hwnd: *mut std::ffi::c_void, pid: *mut u32) -> u32;
    fn GetClassNameW(hwnd: *mut std::ffi::c_void, name: *mut u16, len: i32) -> i32;
    fn SetWindowPos(
        hwnd: *mut std::ffi::c_void,
        after: *mut std::ffi::c_void,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
        flags: u32,
    ) -> i32;
}

#[cfg(windows)]
#[link(name = "gdi32")]
unsafe extern "system" {
    fn CreateRectRgn(left: i32, top: i32, right: i32, bottom: i32) -> *mut std::ffi::c_void;
    fn DeleteObject(object: *mut std::ffi::c_void) -> i32;
}

#[cfg(windows)]
#[link(name = "dwmapi")]
unsafe extern "system" {
    fn DwmEnableBlurBehindWindow(hwnd: *mut std::ffi::c_void, blur: *const DwmBlurBehind) -> i32;
    fn DwmExtendFrameIntoClientArea(hwnd: *mut std::ffi::c_void, margins: *const DwmMargins) -> i32;
}

#[cfg(windows)]
#[link(name = "ntdll")]
unsafe extern "system" {
    fn RtlGetVersion(info: *mut OsVersionInfo) -> i32;
}

// `GetLastError` lives in kernel32, not user32. Call it before any other Windows call.
#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetLastError() -> u32;
    fn GetCurrentProcessId() -> u32;
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
