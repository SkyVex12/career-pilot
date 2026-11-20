//! Optional native background lifecycle. No API client, microphone or transcription access.
//!
//! Integration: call `setup` once, route close events to `on_window_event`, and skip
//! foreground startup work when `background_start_requested()` is true. The frontend
//! must inspect `background_status` before starting its own API/bootstrap work.

#[path = "call_detection.rs"]
mod detection;
#[cfg(target_os = "macos")]
#[path = "call_detection_macos.rs"]
mod macos;

use std::path::{Path, PathBuf};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Window, WindowEvent};

pub(crate) const EVENT: &str = "copilot://background";
const BACKGROUND_ARG: &str = "--background";
const OPEN: &str = "ruby-background-open";
const PAUSE: &str = "ruby-background-pause";
const EXIT: &str = "ruby-background-exit";
#[cfg(any(target_os = "macos", test))]
const AGENT_ID: &str = "com.copilot.desktop.login";

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(crate) struct BackgroundPreferences {
    pub launch_at_login: bool,
    pub keep_running: bool,
    pub suggestions_paused: bool,
    pub call_suggestions: bool,
    pub disabled_apps: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct BackgroundStatus {
    pub preferences: BackgroundPreferences,
    pub background_start: bool,
    pub launch_at_login_supported: bool,
    /// Whether the requested entry is currently present. Does not claim OS policy allows it.
    pub login_entry_present: bool,
    pub call_detection_available: bool,
    pub accessibility_required: bool,
    pub limitation: &'static str,
}

struct BackgroundState {
    preferences: Mutex<BackgroundPreferences>,
    path: PathBuf,
    pause_item: CheckMenuItem<tauri::Wry>,
    background_start: AtomicBool,
    exiting: AtomicBool,
    detector: Mutex<detection::Detector>,
    accepted: Mutex<Option<&'static str>>,
    detection_status: Mutex<DetectionCache>,
}

pub(crate) fn is_background_arguments<'a>(args: impl IntoIterator<Item = &'a str>) -> bool {
    args.into_iter().any(|arg| arg == BACKGROUND_ARG)
}

pub(crate) fn background_start_requested() -> bool {
    std::env::args().skip(1).any(|argument| argument == BACKGROUND_ARG)
}
fn load(path: &Path) -> Result<BackgroundPreferences, String> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| format!("Background settings: {e}")),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(BackgroundPreferences::default()),
        Err(e) => Err(format!("Background settings: {e}")),
    }
}

fn save(path: &Path, preferences: &BackgroundPreferences) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let bytes = serde_json::to_vec_pretty(preferences).map_err(|e| e.to_string())?;
    std::fs::write(path, bytes).map_err(|e| e.to_string())
}

pub(crate) fn open(app: &AppHandle) -> Result<(), String> {
    let main = app.get_webview_window("main").ok_or("Workspace window is unavailable")?;
    main.show().map_err(|e| e.to_string())?;
    main.unminimize().map_err(|e| e.to_string())?;
    main.set_focus().map_err(|e| e.to_string())?;
    if let Some(state) = app.try_state::<BackgroundState>() {
        state.background_start.store(false, Ordering::SeqCst);
        let _ = app.emit(EVENT, ());
    }
    Ok(())
}

/// Keep the existing single-instance plugin; background login launches must not steal focus.
pub(crate) fn second_instance(app: &AppHandle, args: &[String]) {
    if !is_background_arguments(args.iter().map(String::as_str))
        && let Err(error) = open(app)
    {
        tracing::warn!("Could not restore workspace: {error}");
    }
}

pub(crate) fn setup(app: &AppHandle, cleanup_before_exit: fn()) -> Result<(), String> {
    let path = app.path().app_config_dir().map_err(|e| e.to_string())?.join("background.json");
    let preferences = load(&path).unwrap_or_else(|error| {
        tracing::warn!("{error}; background mode remains off");
        BackgroundPreferences::default()
    });
    let open_item =
        MenuItem::with_id(app, OPEN, "Open Copilot", true, None::<&str>).map_err(|e| e.to_string())?;
    let pause_item = CheckMenuItem::with_id(
        app,
        PAUSE,
        "Pause call suggestions",
        true,
        preferences.suggestions_paused,
        None::<&str>,
    )
    .map_err(|e| e.to_string())?;
    let exit_item =
        MenuItem::with_id(app, EXIT, "Exit completely", true, None::<&str>).map_err(|e| e.to_string())?;
    let menu = Menu::with_items(app, &[&open_item, &pause_item, &exit_item]).map_err(|e| e.to_string())?;
    let icon = app.default_window_icon().cloned().ok_or("Ruby app icon is unavailable")?;
    // Register state only after native menu construction succeeds.
    app.manage(BackgroundState {
        preferences: Mutex::new(preferences),
        path,
        pause_item,
        background_start: AtomicBool::new(background_start_requested()),
        exiting: AtomicBool::new(false),
        detector: Mutex::new(detection::Detector::default()),
        accepted: Mutex::new(None),
        detection_status: Mutex::new(DetectionCache::default()),
    });
    TrayIconBuilder::with_id("ruby-background")
        .icon(icon)
        .tooltip("Ruby — Copilot")
        .menu(&menu)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            OPEN => {
                let _ = open(app);
            }
            PAUSE => {
                let state = app.state::<BackgroundState>();
                let result = (|| -> Result<(), String> {
                    let mut prefs = state.preferences.lock().map_err(|e| e.to_string())?;
                    let old = prefs.suggestions_paused;
                    let mut next = prefs.clone();
                    next.suggestions_paused = !old;
                    if let Err(error) = save(&state.path, &next) {
                        drop(prefs);
                        let _ = state.pause_item.set_checked(old);
                        return Err(error);
                    }
                    *prefs = next;
                    let paused = prefs.suggestions_paused;
                    drop(prefs);
                    let _ = state.pause_item.set_checked(paused);
                    let _ = app.emit(EVENT, ());
                    Ok(())
                })();
                if let Err(error) = result {
                    tracing::warn!("Pause suggestions: {error}");
                }
            }
            EXIT => {
                app.state::<BackgroundState>().exiting.store(true, Ordering::SeqCst);
                cleanup_before_exit();
                app.exit(0);
            }
            _ => {}
        })
        .build(app)
        .map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    macos::setup(app.clone());
    start_detection(app.clone());
    if background_start_requested()
        && let Some(main) = app.get_webview_window("main")
    {
        main.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn should_hide_on_close(main: bool, keep_running: bool, exiting: bool) -> bool {
    main && keep_running && !exiting
}

/// Return true when the close was consumed. Preserve all existing handlers otherwise.
pub(crate) fn on_window_event(window: &Window, event: &WindowEvent) -> bool {
    let WindowEvent::CloseRequested { api, .. } = event else { return false };
    let Some(state) = window.app_handle().try_state::<BackgroundState>() else { return false };
    let keep_running = state.preferences.lock().map(|prefs| prefs.keep_running).unwrap_or(false);
    if should_hide_on_close(window.label() == "main", keep_running, state.exiting.load(Ordering::SeqCst)) {
        // If hiding fails, allow the existing normal close behavior.
        if window.hide().is_ok() {
            api.prevent_close();
            return true;
        }
    }
    false
}

#[tauri::command]
pub(crate) fn background_status(app: AppHandle) -> Result<BackgroundStatus, String> {
    let state = app.state::<BackgroundState>();
    let preferences = state.preferences.lock().map_err(|e| e.to_string())?.clone();
    Ok(BackgroundStatus {
        preferences,
        background_start: state.background_start.load(Ordering::SeqCst),
        launch_at_login_supported: cfg!(any(windows, target_os = "macos")),
        login_entry_present: login_entry_present(&app)?,
        call_detection_available: cfg!(any(windows, target_os = "macos")),
        accessibility_required: cfg!(target_os = "macos"),
        limitation: "Windows experimental detection checks accessible call controls and native microphone activity without recording. English control labels only; real app calls are not yet certified. macOS requires Accessibility and notification permission; system notifications respect Focus. macOS full app linking and device behavior are not yet verified. Login changes take effect at the next sign-in; OS policy may disable startup.",
    })
}

#[tauri::command]
pub(crate) fn background_set_preferences(
    app: AppHandle,
    preferences: BackgroundPreferences,
) -> Result<BackgroundStatus, String> {
    let state = app.state::<BackgroundState>();
    {
        let mut current = state.preferences.lock().map_err(|e| e.to_string())?;
        if current.launch_at_login != preferences.launch_at_login {
            set_launch_at_login(&app, preferences.launch_at_login)?;
        }
        if let Err(error) = save(&state.path, &preferences) {
            if current.launch_at_login != preferences.launch_at_login
                && let Err(rollback) = set_launch_at_login(&app, current.launch_at_login)
            {
                return Err(format!("{error}; login rollback failed: {rollback}"));
            }
            return Err(error);
        }
        *current = preferences;
        let paused = current.suggestions_paused;
        drop(current);
        state.pause_item.set_checked(paused).map_err(|e| e.to_string())?;
    }
    let _ = app.emit(EVENT, ());
    background_status(app)
}

#[cfg(windows)]
const RUN_KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";
#[cfg(windows)]
fn reg_command() -> std::process::Command {
    use std::os::windows::process::CommandExt;
    let mut command = std::process::Command::new("reg.exe");
    command.creation_flags(0x08000000); // CREATE_NO_WINDOW: no console flash.
    command
}

#[cfg(any(windows, test))]
fn login_command(executable: &str) -> Result<String, String> {
    if executable.contains(['"', '\n', '\r']) {
        return Err("Executable path cannot be represented safely as a login command".into());
    }
    Ok(format!("\"{executable}\" {BACKGROUND_ARG}"))
}

#[cfg(windows)]
fn set_launch_at_login(_app: &AppHandle, enabled: bool) -> Result<(), String> {
    let mut command = reg_command();
    if enabled {
        let executable = std::env::current_exe().map_err(|e| e.to_string())?;
        let value = login_command(executable.to_str().ok_or("Executable path is not Unicode")?)?;
        command.args(["add", RUN_KEY, "/v", "RubyCopilot", "/t", "REG_SZ", "/d", &value, "/f"]);
    } else {
        if !login_entry_present(_app)? {
            return Ok(());
        }
        command.args(["delete", RUN_KEY, "/v", "RubyCopilot", "/f"]);
    }
    let output = command.output().map_err(|e| e.to_string())?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!("Could not update login startup: {}", String::from_utf8_lossy(&output.stderr)))
    }
}

#[cfg(windows)]
fn login_entry_present(_app: &AppHandle) -> Result<bool, String> {
    let output =
        reg_command().args(["query", RUN_KEY, "/v", "RubyCopilot"]).output().map_err(|e| e.to_string())?;
    Ok(output.status.success())
}

#[cfg(any(target_os = "macos", test))]
fn xml_text(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[cfg(any(target_os = "macos", test))]
fn launch_agent(executable: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{AGENT_ID}</string>
<key>ProgramArguments</key><array><string>{}</string><string>{BACKGROUND_ARG}</string></array>
<key>RunAtLoad</key><true/>
<key>ProcessType</key><string>Background</string>
</dict></plist>
"#,
        xml_text(executable)
    )
}

#[cfg(target_os = "macos")]
fn agent_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .home_dir()
        .map_err(|e| e.to_string())?
        .join("Library/LaunchAgents")
        .join(format!("{AGENT_ID}.plist")))
}

#[cfg(target_os = "macos")]
fn set_launch_at_login(app: &AppHandle, enabled: bool) -> Result<(), String> {
    let path = agent_path(app)?;
    if enabled {
        let executable = std::env::current_exe().map_err(|e| e.to_string())?;
        let executable = executable.to_str().ok_or("Executable path is not Unicode")?;
        std::fs::create_dir_all(path.parent().ok_or("LaunchAgents directory missing")?)
            .map_err(|e| e.to_string())?;
        std::fs::write(path, launch_agent(executable)).map_err(|e| e.to_string())
    } else {
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}

#[cfg(target_os = "macos")]
fn login_entry_present(app: &AppHandle) -> Result<bool, String> {
    Ok(agent_path(app)?.is_file())
}

#[cfg(not(any(windows, target_os = "macos")))]
fn set_launch_at_login(_app: &AppHandle, _enabled: bool) -> Result<(), String> {
    Err("Launch at login is available on Windows and macOS".into())
}
#[cfg(not(any(windows, target_os = "macos")))]
fn login_entry_present(_app: &AppHandle) -> Result<bool, String> {
    Ok(false)
}

/// A native identity primitive, deliberately NOT call evidence. No audio or screen capture.
/// It is event-driven by the caller; this module creates no polling thread.
#[cfg(windows)]
pub(crate) fn foreground_external_process_id() -> Option<u32> {
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId};
    let mut process_id = 0;
    unsafe {
        let foreground = GetForegroundWindow();
        if foreground.0.is_null() {
            return None;
        }
        GetWindowThreadProcessId(foreground, Some(&mut process_id));
    }
    (process_id != 0 && process_id != std::process::id()).then_some(process_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detection_diagnostics_distinguish_policy_evidence_and_delivery() {
        let mut prefs = BackgroundPreferences { call_suggestions: true, ..Default::default() };
        let mut cache = DetectionCache::default();
        assert_eq!(detection_state(true, &prefs, false, &cache, false), "checking");
        cache.diagnostics = Some(detection::DetectionDiagnostics {
            microphone_active: Some(false),
            notifications_allowed: true,
            ..Default::default()
        });
        assert_eq!(detection_state(true, &prefs, false, &cache, false), "waiting_microphone");
        cache.diagnostics.as_mut().unwrap().microphone_active = Some(true);
        assert_eq!(detection_state(true, &prefs, false, &cache, false), "no_call_controls");
        cache.diagnostics.as_mut().unwrap().matched_calls.push("Browser".into());
        assert_eq!(detection_state(true, &prefs, false, &cache, false), "watching");
        prefs.disabled_apps.push("Browser".into());
        assert_eq!(detection_state(true, &prefs, false, &cache, false), "app_disabled");
        prefs.disabled_apps.clear();
        cache.presentation_error = Some("Window creation failed".into());
        assert_eq!(detection_state(true, &prefs, false, &cache, true), "suggestion_pending");
        cache.diagnostics.as_mut().unwrap().notifications_allowed = false;
        assert_eq!(detection_state(true, &prefs, false, &cache, true), "notifications_suppressed");
        assert_eq!(detection_state(true, &prefs, true, &cache, true), "active_session");
        cache.error = Some("Scan failed".into());
        assert_eq!(detection_state(true, &prefs, false, &cache, true), "error");
        prefs.suggestions_paused = true;
        assert_eq!(detection_state(true, &prefs, false, &cache, true), "paused");
        prefs.call_suggestions = false;
        assert_eq!(detection_state(true, &prefs, false, &cache, true), "disabled");
        assert_eq!(detection_state(false, &prefs, false, &cache, true), "unavailable");
    }

    #[test]
    fn popup_retry_restores_hidden_unchanged_offer_but_ignores_expired_or_dismissed_attempts() {
        let pending = Some(detection::Suggestion {
            id: "call:Browser".into(),
            platform: "Browser".into(),
            kind: "start",
        });
        let timely = std::time::Duration::from_millis(100);
        assert!(presentation_attempt_current(&pending, &pending, timely));
        // A failed show leaves the window hidden even though detector state did not change.
        assert!(should_restore_suggestion(false, false, false));
        assert!(should_restore_suggestion(false, true, true));
        assert!(!should_restore_suggestion(false, true, false));
        assert!(!presentation_attempt_current(&pending, &None, timely));
        assert!(!presentation_attempt_current(&pending, &pending, std::time::Duration::from_secs(5)));
        assert!(!presentation_attempt_current(&None, &pending, std::time::Duration::from_secs(6)));
        // Fresh suppression may hide a pending offer without clearing its lifecycle record.
        assert!(presentation_attempt_current(&None, &pending, timely));
    }

    #[test]
    fn suggestion_position_respects_negative_monitors_and_small_work_areas() {
        assert_eq!(suggestion_position((0, 0), (1920, 1040), (340, 200), 16), (1564, 824));
        assert_eq!(suggestion_position((-1920, -200), (1920, 1040), (340, 200), 16), (-356, 624));
        assert_eq!(suggestion_position((50, 60), (200, 100), (340, 200), 16), (50, 60));
        assert_eq!(suggestion_position((i32::MAX, 0), (u32::MAX, 100), (1, 200), 16), (i32::MAX, 0));
    }

    #[test]
    fn explicit_background_arguments_only() {
        assert!(is_background_arguments(["Ruby.exe", "--background"]));
        assert!(!is_background_arguments(["Ruby.exe", "--background=false"]));
        assert!(!is_background_arguments(["Ruby.exe"]));
    }

    #[test]
    fn close_policy_preserves_manual_exit_and_overlay() {
        assert!(should_hide_on_close(true, true, false));
        assert!(!should_hide_on_close(true, false, false));
        assert!(!should_hide_on_close(true, true, true));
        assert!(!should_hide_on_close(false, true, false));
    }

    #[test]
    fn preferences_are_opt_in_and_backward_compatible() {
        let preferences: BackgroundPreferences = serde_json::from_str("{}").unwrap();
        assert_eq!(preferences, BackgroundPreferences::default());
        assert!(!preferences.keep_running);
        assert!(!preferences.launch_at_login);
    }

    #[test]
    fn login_path_is_quoted_and_has_no_shell_interpretation() {
        assert_eq!(
            login_command(r"C:\Program Files\Ruby\Ruby.exe").unwrap(),
            r#""C:\Program Files\Ruby\Ruby.exe" --background"#
        );
        assert!(login_command("bad\"path.exe").is_err());
    }

    #[test]
    fn mac_agent_escapes_paths_and_does_not_keep_alive_or_capture() {
        let text = launch_agent("/Applications/Ruby & Friends.app/Contents/MacOS/Ruby");
        assert!(text.contains("Ruby &amp; Friends.app"));
        assert!(text.contains("<string>--background</string>"));
        assert!(!text.contains("KeepAlive"));
        assert!(!text.contains("--record"));
    }

    #[test]
    fn preferences_roundtrip_without_tokens() {
        let preferences = BackgroundPreferences {
            keep_running: true,
            suggestions_paused: true,
            launch_at_login: false,
            ..Default::default()
        };
        let text = serde_json::to_string(&preferences).unwrap();
        assert_eq!(serde_json::from_str::<BackgroundPreferences>(&text).unwrap(), preferences);
        assert!(!text.contains("token"));
    }
}

/// Diagnostic activity only: neither app identity nor simultaneous input/output proves a call.
#[derive(Debug, Serialize)]
pub(crate) struct NativeActivitySnapshot {
    pub supported: bool,
    pub foreground_external_process_id: Option<u32>,
    pub sessions: Vec<NativeAudioActivity>,
    pub warnings: Vec<String>,
    pub call_evidence: bool,
}

#[derive(Debug, Serialize)]
pub(crate) struct NativeAudioActivity {
    pub process_id: u32,
    pub input_active: bool,
    pub output_active: bool,
}

/// User-requested inspection on a worker thread. Opens COM session-control interfaces,
/// never IAudioClient/IAudioCaptureClient, microphone streams, or backend connections.
#[tauri::command]
pub(crate) async fn background_native_activity() -> Result<NativeActivitySnapshot, String> {
    tauri::async_runtime::spawn_blocking(native_activity_snapshot).await.map_err(|e| e.to_string())?
}

#[cfg(not(windows))]
fn native_activity_snapshot() -> Result<NativeActivitySnapshot, String> {
    Ok(NativeActivitySnapshot {
        supported: false,
        foreground_external_process_id: None,
        sessions: Vec::new(),
        warnings: vec!["Native audio session inspection is not implemented on this platform".into()],
        call_evidence: false,
    })
}

#[cfg(windows)]
fn native_activity_snapshot() -> Result<NativeActivitySnapshot, String> {
    use std::collections::BTreeMap;
    use windows::Win32::Media::Audio::{
        AudioSessionStateActive, DEVICE_STATE_ACTIVE, IAudioSessionControl2, IAudioSessionManager2,
        IMMDeviceEnumerator, MMDeviceEnumerator, eCapture, eRender,
    };
    use windows::Win32::System::Com::{
        CLSCTX_ALL, COINIT_MULTITHREADED, CoCreateInstance, CoInitializeEx, CoUninitialize,
    };
    use windows_core::Interface;

    struct ComGuard;
    impl Drop for ComGuard {
        fn drop(&mut self) {
            unsafe {
                CoUninitialize();
            }
        }
    }
    // This function only runs on a dedicated runtime blocking worker. On an incompatible
    // apartment, report unavailable; do not uninitialize someone else's COM apartment.
    unsafe {
        CoInitializeEx(None, COINIT_MULTITHREADED).ok().map_err(|e| e.to_string())?;
    }
    let _guard = ComGuard;
    let enumerator: IMMDeviceEnumerator =
        unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|e| e.to_string())? };
    let mut processes: BTreeMap<u32, NativeAudioActivity> = BTreeMap::new();
    let mut warnings = Vec::new();
    for (flow, input) in [(eCapture, true), (eRender, false)] {
        let devices =
            unsafe { enumerator.EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE) }.map_err(|e| e.to_string())?;
        let count = unsafe { devices.GetCount() }.map_err(|e| e.to_string())?;
        for index in 0..count {
            let result = (|| -> windows_core::Result<()> {
                let device = unsafe { devices.Item(index)? };
                let manager: IAudioSessionManager2 = unsafe { device.Activate(CLSCTX_ALL, None)? };
                let sessions = unsafe { manager.GetSessionEnumerator()? };
                let session_count = unsafe { sessions.GetCount()? };
                for session_index in 0..session_count {
                    let session = unsafe { sessions.GetSession(session_index)? };
                    if unsafe { session.GetState()? } != AudioSessionStateActive {
                        continue;
                    }
                    let control: IAudioSessionControl2 = session.cast()?;
                    let process_id = unsafe { control.GetProcessId()? };
                    if !external_process(process_id, std::process::id()) {
                        continue;
                    }
                    let entry = processes.entry(process_id).or_insert(NativeAudioActivity {
                        process_id,
                        input_active: false,
                        output_active: false,
                    });
                    if input {
                        entry.input_active = true;
                    } else {
                        entry.output_active = true;
                    }
                }
                Ok(())
            })();
            if let Err(error) = result {
                warnings.push(format!("{} device {index}: {error}", if input { "Input" } else { "Output" }));
            }
        }
    }
    Ok(NativeActivitySnapshot {
        supported: true,
        foreground_external_process_id: foreground_external_process_id(),
        sessions: processes.into_values().collect(),
        warnings,
        call_evidence: false,
    })
}

#[cfg(any(windows, test))]
fn external_process(process_id: u32, own_process_id: u32) -> bool {
    process_id != 0 && process_id != own_process_id
}

#[cfg(test)]
mod native_tests {
    use super::*;

    #[test]
    fn native_identity_never_includes_system_or_own_audio() {
        assert!(!external_process(0, 42));
        assert!(!external_process(42, 42));
        assert!(external_process(43, 42));
    }

    #[test]
    fn preferences_file_roundtrip_and_corruption_are_explicit() {
        let directory = std::env::temp_dir().join(format!("ruby-background-test-{}", uuid::Uuid::now_v7()));
        let path = directory.join("background.json");
        assert_eq!(load(&path).unwrap(), BackgroundPreferences::default());
        let expected = BackgroundPreferences {
            keep_running: true,
            launch_at_login: false,
            suggestions_paused: true,
            ..Default::default()
        };
        save(&path, &expected).unwrap();
        assert_eq!(load(&path).unwrap(), expected);
        std::fs::write(&path, b"{broken").unwrap();
        assert!(load(&path).is_err());
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    #[ignore = "requires the Windows audio service; run explicitly for host diagnostics"]
    fn windows_audio_session_inspection_does_not_claim_calls() {
        let snapshot = std::thread::spawn(native_activity_snapshot).join().unwrap().unwrap();
        assert!(snapshot.supported);
        assert!(!snapshot.call_evidence);
        assert!(snapshot.sessions.iter().all(|session| session.process_id != std::process::id()));
        println!(
            "Windows WASAPI inspection: {} external process records, {} device warnings; no call evidence",
            snapshot.sessions.len(),
            snapshot.warnings.len()
        );
    }
}

const SUGGESTION_EVENT: &str = "copilot://call-suggestion";

#[derive(Clone, Default)]
struct DetectionCache {
    last_checked_unix_ms: Option<u64>,
    diagnostics: Option<detection::DetectionDiagnostics>,
    error: Option<String>,
    presentation_error: Option<String>,
}

#[derive(Serialize)]
pub(crate) struct DetectionStatus {
    preferences: BackgroundPreferences,
    active_session: bool,
    state: &'static str,
    last_checked_unix_ms: Option<u64>,
    diagnostics: Option<detection::DetectionDiagnostics>,
    error: Option<String>,
    presentation_error: Option<String>,
    pending: Option<detection::Suggestion>,
}

fn active_session(app: &AppHandle) -> bool {
    app.try_state::<crate::AppState>().is_some_and(|s| {
        s.gate.starting()
            || s.engine.lock().map(|e| e.is_some()).unwrap_or(true)
            || s.seat.lock().map(|e| e.is_some()).unwrap_or(true)
    })
}

fn detection_state(
    supported: bool,
    prefs: &BackgroundPreferences,
    active: bool,
    cache: &DetectionCache,
    pending: bool,
) -> &'static str {
    if !supported {
        return "unavailable";
    }
    if !prefs.call_suggestions {
        return "disabled";
    }
    if prefs.suggestions_paused {
        return "paused";
    }
    if cache.error.is_some() {
        return "error";
    }
    if active {
        return "active_session";
    }
    let Some(diagnostics) = &cache.diagnostics else {
        return "checking";
    };
    if !diagnostics.notifications_allowed {
        return "notifications_suppressed";
    }
    if pending {
        return "suggestion_pending";
    }
    if !diagnostics.matched_calls.is_empty()
        && diagnostics.matched_calls.iter().all(|app| prefs.disabled_apps.contains(app))
    {
        return "app_disabled";
    }
    if diagnostics.microphone_active == Some(false) {
        return "waiting_microphone";
    }
    if diagnostics.matched_calls.is_empty() {
        return "no_call_controls";
    }
    "watching"
}

/// Read the most recent background scan; never trigger a second accessibility scan.
#[tauri::command]
pub(crate) fn background_detection_status(app: AppHandle) -> Result<DetectionStatus, String> {
    let state = app.state::<BackgroundState>();
    let preferences = state.preferences.lock().map_err(|e| e.to_string())?.clone();
    let cache = state.detection_status.lock().map_err(|e| e.to_string())?.clone();
    let pending = state.detector.lock().map_err(|e| e.to_string())?.pending.clone();
    let active_session = active_session(&app);
    Ok(DetectionStatus {
        state: detection_state(
            cfg!(any(windows, target_os = "macos")),
            &preferences,
            active_session,
            &cache,
            pending.is_some(),
        ),
        preferences,
        active_session,
        pending,
        last_checked_unix_ms: cache.last_checked_unix_ms,
        diagnostics: cache.diagnostics,
        error: cache.error,
        presentation_error: cache.presentation_error,
    })
}

#[cfg(any(not(target_os = "macos"), test))]
fn suggestion_position(origin: (i32, i32), area: (u32, u32), size: (u32, u32), margin: i32) -> (i32, i32) {
    // Physical coordinates support negative monitor origins and mixed DPI layouts.
    let coordinate = |start: i32, available: u32, extent: u32| {
        (i64::from(start) + (i64::from(available) - i64::from(extent) - i64::from(margin)).max(0))
            .clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32
    };
    (coordinate(origin.0, area.0, size.0), coordinate(origin.1, area.1, size.1))
}

#[cfg(any(not(target_os = "macos"), test))]
fn should_restore_suggestion(changed: bool, visible: bool, minimized: bool) -> bool {
    changed || !visible || minimized
}

#[cfg(any(not(target_os = "macos"), test))]
fn presentation_attempt_current(
    expected: &Option<detection::Suggestion>,
    current: &Option<detection::Suggestion>,
    age: std::time::Duration,
) -> bool {
    age < std::time::Duration::from_secs(5) && (expected.is_none() || current == expected)
}

#[cfg(not(target_os = "macos"))]
fn present_suggestion(
    app: &AppHandle,
    pending: Option<detection::Suggestion>,
    changed: bool,
) -> Result<(), String> {
    if pending.is_none() && app.get_webview_window("call-suggestion").is_none() {
        return Ok(());
    }
    // Creation marshals to the UI thread. Never hold state locks while waiting for it.
    let window = match app.get_webview_window("call-suggestion") {
        Some(window) => window,
        None => tauri::WebviewWindowBuilder::new(
            app,
            "call-suggestion",
            tauri::WebviewUrl::App("index.html?call-suggestion".into()),
        )
        .title("Call detected")
        .inner_size(340.0, 170.0)
        .resizable(false)
        .minimizable(false)
        .maximizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .closable(false)
        .focused(false)
        .visible(false)
        .build()
        .map_err(|e| format!("Could not create call suggestion: {e}"))?,
    };
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let handle = app.clone();
    let queued = std::time::Instant::now();
    app.run_on_main_thread(move || {
        let result = (|| -> Result<(), String> {
            // A response cannot interleave on the UI thread between this validation and show.
            // Release every state lock before invoking any native window operation.
            let state = handle.state::<BackgroundState>();
            let current = state.detector.lock().map_err(|e| e.to_string())?.pending.clone();
            if !presentation_attempt_current(&pending, &current, queued.elapsed()) {
                return Ok(());
            }
            let prefs = state.preferences.lock().map_err(|e| e.to_string())?.clone();
            let allowed = prefs.call_suggestions
                && !prefs.suggestions_paused
                && pending.as_ref().is_some_and(|s| {
                    !prefs.disabled_apps.contains(&s.platform)
                        && (s.kind != "start" || !active_session(&handle))
                });
            if !allowed {
                window.hide().map_err(|e| format!("Could not hide call suggestion: {e}"))?;
                return Ok(());
            }
            if should_restore_suggestion(
                changed,
                window.is_visible().map_err(|e| e.to_string())?,
                window.is_minimized().map_err(|e| e.to_string())?,
            ) {
                let monitor = handle
                    .get_webview_window("main")
                    .and_then(|main| main.current_monitor().ok().flatten())
                    .or(window.primary_monitor().map_err(|e| e.to_string())?);
                if let Some(monitor) = monitor {
                    let area = monitor.work_area();
                    let size = window.outer_size().map_err(|e| e.to_string())?;
                    let (x, y) = suggestion_position(
                        (area.position.x, area.position.y),
                        (area.size.width, area.size.height),
                        (size.width, size.height),
                        (16.0 * monitor.scale_factor()).round() as i32,
                    );
                    window.set_position(tauri::PhysicalPosition::new(x, y)).map_err(|e| e.to_string())?;
                } else {
                    window.center().map_err(|e| e.to_string())?;
                }
                // A delayed callback must not revive a timed-out delivery attempt.
                if !presentation_attempt_current(&pending, &current, queued.elapsed()) {
                    return Ok(());
                }
                window.unminimize().map_err(|e| e.to_string())?;
                window.show().map_err(|e| format!("Could not show call suggestion: {e}"))?;
            }
            Ok(())
        })();
        let _ = sender.send(result);
    })
    .map_err(|e| e.to_string())?;
    receiver
        .recv_timeout(std::time::Duration::from_secs(5))
        .map_err(|_| "Call suggestion window did not respond; retrying".to_string())?
}

fn start_detection(app: AppHandle) {
    std::thread::spawn(move || {
        let started = std::time::Instant::now();
        loop {
            std::thread::sleep(std::time::Duration::from_secs(5));
            let state = app.state::<BackgroundState>();
            if state.exiting.load(Ordering::SeqCst) {
                break;
            }
            let prefs = match state.preferences.lock() {
                Ok(p) => p.clone(),
                Err(_) => continue,
            };
            let enabled = cfg!(any(windows, target_os = "macos"))
                && prefs.call_suggestions
                && !prefs.suggestions_paused;
            let result = if enabled {
                Some(detection::sample_with_tracking(
                    state.detector.lock().map(|d| d.tracking()).unwrap_or(false),
                ))
            } else {
                None
            };
            let active = active_session(&app);
            let mut cache = DetectionCache {
                last_checked_unix_ms: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .ok()
                    .map(|t| t.as_millis() as u64),
                ..Default::default()
            };
            let mut detector = match state.detector.lock() {
                Ok(d) => d,
                Err(_) => continue,
            };
            let before = detector.pending.clone();
            detector.suppress_apps(&prefs.disabled_apps);
            let mut can_present = false;
            match result {
                Some(Ok(mut sample)) => {
                    sample.evidence.retain(|e| !prefs.disabled_apps.contains(&e.app));
                    can_present = sample.notifications;
                    detector.tick(
                        started.elapsed().as_secs(),
                        &sample.evidence,
                        sample.notifications,
                        active,
                    );
                    cache.diagnostics = Some(sample.diagnostics);
                }
                Some(Err(error)) => {
                    // Keep an unresponded offer for recovery, but never display using stale OS policy.
                    cache.error = Some(error);
                }
                None => {
                    // Defer only unanswered offers; explicit dismissals remain remembered.
                    detector.defer_pending();
                }
            }
            let next = detector.pending.clone();
            let changed = before != next;
            drop(detector);
            // Failed delivery retries next poll without resetting dismissal state.
            #[cfg(not(target_os = "macos"))]
            if let Err(error) =
                present_suggestion(&app, if can_present { next.clone() } else { None }, changed)
            {
                tracing::warn!("Call suggestion presentation failed: {error}");
                cache.presentation_error = Some(error);
            }
            #[cfg(target_os = "macos")]
            if changed || !can_present {
                macos::notify(if can_present { next.as_ref() } else { None });
            }
            // Resend pending state too: a transient event delivery failure must be recoverable.
            if changed || next.is_some() {
                let _ = app.emit(SUGGESTION_EVENT, &next);
            }
            if let Ok(mut current) = state.detection_status.lock() {
                *current = cache;
            }
        }
    });
}

#[tauri::command]
pub(crate) fn background_suggestion(app: AppHandle) -> Result<Option<detection::Suggestion>, String> {
    let state = app.state::<BackgroundState>();
    let result = state.detector.lock().map_err(|e| e.to_string())?.pending.clone();
    Ok(result)
}

#[tauri::command]
pub(crate) fn background_respond(app: AppHandle, id: String, accept: bool) -> Result<(), String> {
    let state = app.state::<BackgroundState>();
    let suggestion =
        state.detector.lock().map_err(|e| e.to_string())?.respond(&id, accept).ok_or("Suggestion expired")?;
    if let Some(window) = app.get_webview_window("call-suggestion") {
        let _ = window.hide();
    }
    #[cfg(target_os = "macos")]
    macos::notify(None);
    let _ = app.emit(SUGGESTION_EVENT, Option::<detection::Suggestion>::None);
    if accept {
        *state.accepted.lock().map_err(|e| e.to_string())? = Some(suggestion.kind);
        open(&app)?;
        app.emit("copilot://call-accepted", suggestion.kind).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn background_take_accepted(app: AppHandle) -> Result<Option<&'static str>, String> {
    let state = app.state::<BackgroundState>();
    let result = state.accepted.lock().map_err(|e| e.to_string())?.take();
    Ok(result)
}

#[tauri::command]
pub(crate) async fn background_request_permissions() -> Result<bool, String> {
    #[cfg(target_os = "macos")]
    return tauri::async_runtime::spawn_blocking(|| -> Result<bool, String> {
        macos::request_accessibility()?;
        let notifications = macos::request_notifications()?;
        Ok(notifications && macos::trusted())
    })
    .await
    .map_err(|e| e.to_string())?;
    #[cfg(not(target_os = "macos"))]
    Err("Extra background permissions are only required on macOS".into())
}
