//! Stealth mode: keep every app window visible and usable on the monitor, and leave it out of
//! screen capture that honors window display affinity.
//!
//! On Windows 10 2004 and later this is `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
//! That flag omits the window. `WDA_MONITOR` (0x1) is never used: it paints a black rectangle
//! in the capture. If the call is rejected (older Windows, a window the flag does not cover,
//! or a capture path that ignores affinity), the window stays as it is and the failure is logged.
//! Some Windows 11 21H2 systems reject exclusion with ERROR_NOT_ENOUGH_MEMORY. Report the
//! observed affinity instead of reparenting WebView2: destroying a temporary parent after a
//! failed restore can destroy the live WebView, and reparenting during resize is unsafe.
//!
//! While it is on, the main window also leaves the taskbar. It stays open and usable. The main
//! window title is the configured display name. Task Manager's process name is the executable's
//! description, which `program_name` writes when that name is saved; it shows up after the next
//! start. The executable file name stays `copilot-desktop.exe`.
//!
//! Affinity and transparent DWM surfaces are updated on the same window thread. Hidden windows
//! must not receive presentation or affinity changes: Windows can paint their dormant surface
//! as a white rectangle. `show` reconciles the latest choice before making a window visible, so
//! deferring those changes never restores stale capture exclusion.

use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, WebviewWindow};
use uuid::Uuid;

/// Stops a focus event raised by applying stealth from applying it again.
static APPLYING: AtomicBool = AtomicBool::new(false);

/// A capture request and the state actually reported by the native platform. Windows readback
/// verifies the flag, not whether a particular third-party capture client honors it.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CaptureExclusionStatus {
    pub window: String,
    pub requested: bool,
    pub effective: Option<bool>,
    pub windows_build: u32,
    pub error: Option<String>,
    pub reason: &'static str,
}

#[derive(Clone)]
struct CapturePolicy {
    stealth: bool,
    display_name: String,
    screenshot_leases: HashSet<Uuid>,
    revision: u64,
}

impl Default for CapturePolicy {
    fn default() -> Self {
        Self {
            stealth: false,
            display_name: DEFAULT_DISPLAY_NAME.into(),
            screenshot_leases: HashSet::new(),
            revision: 0,
        }
    }
}

impl CapturePolicy {
    fn request(&self, label: &str) -> (bool, &'static str) {
        if label == "overlay" && !self.screenshot_leases.is_empty() {
            (true, "screenshot")
        } else if self.stealth {
            (true, "stealth")
        } else {
            (false, "off")
        }
    }

    fn release(&mut self, token: Uuid) {
        if self.screenshot_leases.remove(&token) {
            self.revision += 1;
        }
    }
}

fn policy() -> &'static Mutex<CapturePolicy> {
    static POLICY: OnceLock<Mutex<CapturePolicy>> = OnceLock::new();
    POLICY.get_or_init(|| Mutex::new(CapturePolicy::default()))
}

#[derive(Default)]
struct PresentationCache(BTreeMap<String, (bool, String)>);

impl PresentationCache {
    fn changed(&mut self, label: &str, on: bool, name: &str) -> bool {
        if self.0.get(label).is_some_and(|(was_on, was_name)| *was_on == on && was_name == name) {
            return false;
        }
        self.0.insert(label.into(), (on, name.into()));
        true
    }
}

fn presentation_changed(label: &str, on: bool, name: &str) -> bool {
    static PRESENTATION: OnceLock<Mutex<PresentationCache>> = OnceLock::new();
    PRESENTATION
        .get_or_init(|| Mutex::new(PresentationCache::default()))
        .lock()
        .unwrap()
        .changed(label, on, name)
}

fn statuses() -> &'static Mutex<BTreeMap<String, CaptureExclusionStatus>> {
    static STATUS: OnceLock<Mutex<BTreeMap<String, CaptureExclusionStatus>>> = OnceLock::new();
    STATUS.get_or_init(|| Mutex::new(BTreeMap::new()))
}

#[tauri::command]
pub fn capture_exclusion_status() -> Vec<CaptureExclusionStatus> {
    statuses().lock().unwrap().values().cloned().collect()
}

fn report(
    app: &AppHandle,
    label: &str,
    requested: bool,
    reason: &'static str,
    effective: Option<bool>,
    error: Option<String>,
) {
    let status = CaptureExclusionStatus {
        window: label.into(),
        requested,
        effective,
        windows_build: build_number(),
        error,
        reason,
    };
    let changed = {
        let mut statuses = statuses().lock().unwrap();
        if statuses.get(label) == Some(&status) {
            false
        } else {
            statuses.insert(label.into(), status.clone());
            true
        }
    };
    if changed {
        if let Some(error) = &status.error {
            tracing::warn!(
                window = label,
                requested,
                ?effective,
                reason,
                windows_build = status.windows_build,
                "capture exclusion: {error}"
            );
        } else {
            tracing::info!(
                window = label,
                requested,
                ?effective,
                reason,
                windows_build = status.windows_build,
                "capture exclusion updated"
            );
        }
        let _ = app.emit("capture-exclusion-status", &status);
    }
}

/// `WDA_NONE`: capture includes the window again.
const WDA_NONE: u32 = 0x0000_0000;
/// `WDA_EXCLUDEFROMCAPTURE`: the window stays on the monitor and is left out of capture.
const WDA_EXCLUDEFROMCAPTURE: u32 = 0x0000_0011;

/// Shown in Task Manager for this app. Neutral, and the app's own name.
pub const DEFAULT_DISPLAY_NAME: &str = "CoRuby";
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
        return Err("Enter a short name for this app, such as CoRuby.".into());
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
        return Err("That name belongs to Windows. Use a plain name for this app, such as CoRuby.".into());
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
        (22000..WINDOWS_11_22H2).contains(&windows_build())
    }
    #[cfg(not(windows))]
    {
        false
    }
}

/// Apply the latest persisted choice. In particular, an older settings worker cannot enqueue
/// its stale ON value after a newer worker has saved OFF.
pub fn apply(app: &AppHandle, on: bool, display_name: &str) {
    fn update_choice(on: bool, display_name: &str) {
        let display_name =
            normalize_display_name(display_name).unwrap_or_else(|_| DEFAULT_DISPLAY_NAME.into());
        let mut policy = policy().lock().unwrap();
        if policy.stealth != on || policy.display_name != display_name {
            policy.stealth = on;
            policy.display_name = display_name;
            policy.revision += 1;
        }
    }
    if let Some(state) = app.try_state::<crate::AppState>() {
        // Hold settings until its choice reaches the policy, so an older worker cannot
        // overwrite a newer save between taking a snapshot and updating the request.
        let settings = state.settings.lock().unwrap();
        update_choice(settings.stealth, &settings.display_name);
    } else {
        update_choice(on, display_name);
    }
    refresh(app);
}

/// Reconcile on the window thread. Every native callback reads the current policy instead of
/// capturing a bool that can become stale while the callback waits in the event queue.
pub fn refresh(app: &AppHandle) {
    let handle = app.clone();
    if let Err(error) = app.run_on_main_thread(move || apply_latest(&handle)) {
        tracing::warn!("capture exclusion: could not reach the window thread: {error}");
    }
}

fn apply_latest(app: &AppHandle) {
    if APPLYING.swap(true, Ordering::AcqRel) {
        return;
    }
    loop {
        let current = policy().lock().unwrap().clone();
        #[cfg(target_os = "macos")]
        if presentation_changed("app", current.stealth, &current.display_name)
            && let Err(error) = app.set_activation_policy(if current.stealth {
                tauri::ActivationPolicy::Accessory
            } else {
                tauri::ActivationPolicy::Regular
            })
        {
            tracing::warn!("stealth mode: could not change Dock/app-switcher visibility: {error}");
        }
        for (label, window) in app.webview_windows() {
            if window.is_visible().unwrap_or(false) {
                // Focus/readback must not rewrite native styles and disturb a resize or
                // maximized window. Presentation changes only when its own inputs change.
                present_latest(&label, &window, &current);
            }
            set_window(app, &label, &window);
        }
        if policy().lock().unwrap().revision == current.revision {
            break;
        }
    }
    APPLYING.store(false, Ordering::Release);
}

fn present_latest(label: &str, window: &WebviewWindow, current: &CapturePolicy) {
    if presentation_changed(label, current.stealth, &current.display_name) {
        present(label, window, current.stealth, &current.display_name);
    }
}

/// Every app-owned show path comes here, including call transitions, tray and keyboard actions.
/// Keep reconciliation and showing in one window-thread operation. In particular, OFF saved
/// while an overlay is hidden must clear its old exclusion before the first visible frame.
pub(crate) fn show(window: &WebviewWindow) -> Result<(), String> {
    let visible_window = window.clone();
    let (sender, receiver) = std::sync::mpsc::channel();
    window
        .run_on_main_thread(move || {
            let app = visible_window.app_handle();
            let label = visible_window.label();
            let current = policy().lock().unwrap().clone();
            // Presenting a minimized main window may restore it. Reconcile first as well
            // as after presentation so even that restore cannot expose a stale setting.
            #[cfg(windows)]
            apply_native_window(app, label, &visible_window, true);
            present_latest(label, &visible_window, &current);
            #[cfg(windows)]
            apply_native_window(app, label, &visible_window, true);
            #[cfg(not(windows))]
            set_window(app, label, &visible_window);
            let _ = sender.send(visible_window.show().map_err(|error| error.to_string()));
        })
        .map_err(|error| error.to_string())?;
    receiver.recv().map_err(|_| "The window show operation was interrupted.".to_string())?
}

/// A bounded screenshot temporarily excludes the overlay from its own capture. Once it
/// finishes, the current Stealth choice applies again. Hosting or assisting never overrides it.
pub struct ScreenshotProtection {
    app: AppHandle,
    token: Uuid,
}

impl Drop for ScreenshotProtection {
    fn drop(&mut self) {
        policy().lock().unwrap().release(self.token);
        refresh_overlay(&self.app);
    }
}

pub fn protect_screenshot_overlay(app: &AppHandle) -> ScreenshotProtection {
    let token = Uuid::now_v7();
    {
        let mut policy = policy().lock().unwrap();
        policy.screenshot_leases.insert(token);
        policy.revision += 1;
    }
    refresh_overlay(app);
    ScreenshotProtection { app: app.clone(), token }
}

fn refresh_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("overlay") {
        set_window(app, "overlay", &window);
    }
}

/// Visible workspace and live overlay both have a taskbar button unless Stealth is on.
/// present_latest/show call this only at the safe presentation boundary.
fn present(label: &str, window: &WebviewWindow, on: bool, display_name: &str) {
    if label != "main" && label != "overlay" {
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
fn set_window(app: &AppHandle, label: &str, window: &WebviewWindow) {
    let handle = app.clone();
    let native_window = window.clone();
    let native_label = label.to_string();
    // Recheck visibility on the window thread: an earlier call may have hidden it meanwhile.
    if let Err(error) = app.run_on_main_thread(move || {
        apply_native_window(&handle, &native_label, &native_window, false);
    }) {
        let (requested, reason) = policy().lock().unwrap().request(label);
        report(
            app,
            label,
            requested,
            reason,
            None,
            Some(format!("The window thread is unavailable: {error}")),
        );
    }
}

#[cfg(windows)]
fn apply_native_window(app: &AppHandle, label: &str, window: &WebviewWindow, showing: bool) {
    let (requested, reason) = policy().lock().unwrap().request(label);
    let result = match window.hwnd() {
        Ok(hwnd) => reconcile_affinity(hwnd.0, label, requested, showing),
        Err(error) => (None, Some(format!("The window handle is unavailable: {error}"))),
    };
    report(app, label, requested, reason, result.0, result.1);
}

#[cfg(windows)]
fn reconcile_affinity(
    hwnd: *mut std::ffi::c_void,
    label: &str,
    requested: bool,
    showing: bool,
) -> (Option<bool>, Option<String>) {
    // Even a successful affinity call can materialize a hidden WebView2/DWM surface. Report
    // the pending choice without claiming native verification until the window is shown.
    if !showing && unsafe { IsWindowVisible(hwnd) } == 0 {
        return (None, None);
    }
    let desired = affinity(requested);
    let before = current_affinity(hwnd);
    let error = if before != Some(desired) { set_affinity(hwnd, desired) } else { None };
    let kept = current_affinity(hwnd);
    if matches!(label, "main" | "overlay") && before != kept {
        restore_transparency(hwnd);
    }
    verify_affinity(desired, current_affinity(hwnd), error)
}

#[cfg(windows)]
fn current_affinity(hwnd: *mut std::ffi::c_void) -> Option<u32> {
    let mut kept = 0u32;
    (unsafe { GetWindowDisplayAffinity(hwnd, &mut kept) } != 0).then_some(kept)
}

#[cfg(any(windows, test))]
fn verify_affinity(desired: u32, kept: Option<u32>, error: Option<u32>) -> (Option<bool>, Option<String>) {
    let effective = kept.map(|value| value == WDA_EXCLUDEFROMCAPTURE);
    let error = if kept == Some(desired) {
        None
    } else if let Some(error) = error {
        Some(format!("Windows could not change capture visibility (error {error})."))
    } else if let Some(kept) = kept {
        Some(format!("Windows kept capture affinity {kept:#x} instead of {desired:#x}."))
    } else {
        Some("Windows could not verify capture visibility.".into())
    };
    (effective, error)
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

/// Capture affinity clears the blur region Tao used to make a window transparent. Both the
/// workspace and overlay use transparency, so repair either surface after an affinity change.
#[cfg(windows)]
fn restore_transparency(hwnd: *mut std::ffi::c_void) {
    unsafe {
        let region = CreateRectRgn(0, 0, -1, -1);
        if region.is_null() {
            return;
        }
        let blur =
            DwmBlurBehind { flags: DWM_BB_ENABLE | DWM_BB_BLURREGION, enable: 1, region, transition: 0 };
        let _ = DwmEnableBlurBehindWindow(hwnd, &blur);
        let _ = DeleteObject(region);
        // Build 22000 paints the client area opaque white unless the glass frame covers it.
        // Later Windows already stays transparent from the blur region alone.
        if windows_build() < WINDOWS_11_22H2 {
            let margins = DwmMargins { left: -1, right: -1, top: -1, bottom: -1 };
            let _ = DwmExtendFrameIntoClientArea(hwnd, &margins);
        }
    }
}

#[cfg(windows)]
const WINDOWS_11_22H2: u32 = 22621;

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
    fn IsWindowVisible(hwnd: *mut std::ffi::c_void) -> i32;
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
}

#[cfg(target_os = "macos")]
fn set_window(app: &AppHandle, label: &str, window: &WebviewWindow) {
    let label = label.to_string();
    let native_label = label.clone();
    let native_window = window.clone();
    let handle = app.clone();
    let queued = window.with_webview(move |webview| {
        let (enabled, reason) = policy().lock().unwrap().request(&native_label);
        let legacy_error = native_window.set_content_protected(enabled).err();
        let mask_enabled = enabled && std::env::var("COPILOT_DISABLE_CAPTURE_MASK").as_deref() != Ok("1");
        let result = unsafe { copilot_capture_mask_set(webview.ns_window(), u8::from(mask_enabled)) };
        let error = if result != 0 {
            Some(format!("The experimental capture mask failed ({result})."))
        } else {
            legacy_error.map(|error| format!("Content protection failed: {error}"))
        };
        // macOS has no API here that verifies what a meeting capture client sees.
        report(&handle, &native_label, enabled, reason, None, error);
    });
    if let Err(error) = queued {
        let (enabled, reason) = policy().lock().unwrap().request(&label);
        report(
            app,
            &label,
            enabled,
            reason,
            None,
            Some(format!("The window thread is unavailable: {error}")),
        );
    }
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn copilot_capture_mask_set(window: *mut std::ffi::c_void, enabled: u8) -> i32;
}

#[cfg(not(any(windows, target_os = "macos")))]
fn set_window(app: &AppHandle, label: &str, _window: &WebviewWindow) {
    let (enabled, reason) = policy().lock().unwrap().request(label);
    report(
        app,
        label,
        enabled,
        reason,
        Some(false),
        enabled.then(|| "This system does not support capture exclusion.".into()),
    );
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
    fn focus_and_capture_rechecks_do_not_rewrite_window_presentation() {
        let mut presentation = super::PresentationCache::default();
        assert!(presentation.changed("main", false, "CoRuby"));
        assert!(!presentation.changed("main", false, "CoRuby"));
        assert!(presentation.changed("overlay", false, "CoRuby"));
        assert!(!presentation.changed("main", false, "CoRuby"));
        assert!(presentation.changed("main", true, "CoRuby"));
        assert!(!presentation.changed("main", true, "CoRuby"));
        assert!(presentation.changed("main", false, "CoRuby"));
        assert!(presentation.changed("main", false, "CoRuby Preview"));
    }

    #[test]
    fn ending_a_screenshot_uses_the_latest_stealth_choice() {
        let mut policy = super::CapturePolicy { stealth: true, ..Default::default() };
        let capture = uuid::Uuid::from_u128(1);
        policy.screenshot_leases.insert(capture);
        policy.stealth = false;
        assert_eq!(policy.request("overlay"), (true, "screenshot"));
        policy.release(capture);
        assert_eq!(policy.request("overlay"), (false, "off"));
        assert_eq!(policy.request("main"), (false, "off"));
        // The opposite order must keep a user's newly enabled choice too.
        policy.screenshot_leases.insert(capture);
        policy.stealth = true;
        policy.release(capture);
        assert_eq!(policy.request("overlay"), (true, "stealth"));
    }

    #[test]
    fn stealth_off_is_authoritative_for_host_and_assistant_windows() {
        // Both session roles use these same windows. There is deliberately no lasting
        // role-based exclusion: joining, leaving, or reconnecting cannot override OFF.
        let mut policy = super::CapturePolicy { stealth: true, ..Default::default() };
        for window in ["main", "overlay"] {
            assert_eq!(policy.request(window), (true, "stealth"));
        }
        policy.stealth = false;
        for window in ["main", "overlay"] {
            assert_eq!(policy.request(window), (false, "off"));
        }
        let capture = uuid::Uuid::from_u128(1);
        policy.screenshot_leases.insert(capture);
        assert_eq!(policy.request("overlay"), (true, "screenshot"));
        policy.release(capture);
        assert_eq!(policy.request("overlay"), (false, "off"));
    }

    #[test]
    fn overlapping_screenshots_release_only_their_own_exclusion() {
        let mut policy = super::CapturePolicy::default();
        let first = uuid::Uuid::from_u128(1);
        let second = uuid::Uuid::from_u128(2);
        policy.screenshot_leases.extend([first, second]);
        policy.release(first);
        assert_eq!(policy.request("overlay"), (true, "screenshot"));
        assert_eq!(policy.request("main"), (false, "off"));
        policy.release(first);
        assert_eq!(policy.request("overlay"), (true, "screenshot"));
        policy.release(second);
        assert_eq!(policy.request("overlay"), (false, "off"));
    }

    #[test]
    fn failed_off_reports_retained_exclusion_instead_of_claiming_it_cleared() {
        let (effective, error) = super::verify_affinity(0, Some(0x11), Some(8));
        assert_eq!(effective, Some(true));
        assert!(error.unwrap().contains("error 8"));
        let (effective, error) = super::verify_affinity(0x11, Some(0), Some(8));
        assert_eq!(effective, Some(false));
        assert!(error.is_some());
    }

    #[test]
    fn successful_call_without_matching_readback_is_not_reported_as_success() {
        assert!(super::verify_affinity(0x11, Some(0), None).1.is_some());
        assert!(super::verify_affinity(0, Some(0x11), None).1.is_some());
        assert_eq!(super::verify_affinity(0, Some(0), None), (Some(false), None));
        assert_eq!(super::verify_affinity(0x11, Some(0x11), None), (Some(true), None));
        assert!(super::verify_affinity(0, None, None).1.is_some());
    }

    #[cfg(windows)]
    #[test]
    fn hidden_native_windows_defer_affinity_until_show_and_use_the_latest_choice() {
        use std::ffi::c_void;
        #[link(name = "user32")]
        unsafe extern "system" {
            fn CreateWindowExW(
                ex: u32,
                class: *const u16,
                name: *const u16,
                style: u32,
                x: i32,
                y: i32,
                width: i32,
                height: i32,
                parent: *mut c_void,
                menu: *mut c_void,
                instance: *mut c_void,
                param: *mut c_void,
            ) -> *mut c_void;
            fn DestroyWindow(window: *mut c_void) -> i32;
            fn IsWindowVisible(window: *mut c_void) -> i32;
            fn SetLayeredWindowAttributes(window: *mut c_void, color: u32, alpha: u8, flags: u32) -> i32;
        }
        struct HiddenWindow(*mut c_void);
        impl Drop for HiddenWindow {
            fn drop(&mut self) {
                unsafe {
                    DestroyWindow(self.0);
                }
            }
        }
        let class: Vec<u16> = "STATIC\0".encode_utf16().collect();
        let null = std::ptr::null_mut();
        let window = HiddenWindow(unsafe {
            CreateWindowExW(
                0x0008_0080,
                class.as_ptr(),
                std::ptr::null(),
                0x8000_0000,
                0,
                0,
                16,
                16,
                null,
                null,
                null,
                null,
            )
        });
        assert!(!window.0.is_null());
        assert_ne!(unsafe { SetLayeredWindowAttributes(window.0, 0, 255, 2) }, 0);
        assert_eq!(unsafe { IsWindowVisible(window.0) }, 0);
        for label in ["main", "overlay"] {
            // A hidden ON window is not touched by OFF. It must become capturable before
            // the subsequent show, without creating or reparenting a second native window.
            assert_eq!(super::set_affinity(window.0, super::affinity(true)), None);
            assert_eq!(super::reconcile_affinity(window.0, label, false, false), (None, None));
            assert_eq!(super::current_affinity(window.0), Some(super::affinity(true)));
            assert_eq!(super::reconcile_affinity(window.0, label, false, true), (Some(false), None));
            assert_eq!(super::current_affinity(window.0), Some(super::affinity(false)));
            assert_eq!(unsafe { IsWindowVisible(window.0) }, 0);

            // The reverse transition is equally deferred and applied before presentation.
            assert_eq!(super::reconcile_affinity(window.0, label, true, false), (None, None));
            assert_eq!(super::current_affinity(window.0), Some(super::affinity(false)));
            assert_eq!(super::reconcile_affinity(window.0, label, true, true), (Some(true), None));
            assert_eq!(unsafe { IsWindowVisible(window.0) }, 0);
        }
    }

    #[test]
    fn the_display_name_is_this_app_and_not_a_windows_component() {
        assert_eq!(super::normalize_display_name("  CoRuby  ").unwrap(), "CoRuby");
        assert_eq!(super::normalize_display_name("Copilot Desktop").unwrap(), "Copilot Desktop");
        assert!(super::normalize_display_name("  ").is_err());
        assert!(super::normalize_display_name("svchost.exe").is_err());
        assert!(super::normalize_display_name("Windows Explorer").is_err());
    }
}
