//! Optional native background lifecycle. No API client, microphone or transcription access.
//!
//! Integration: call `setup` once, route close events to `on_window_event`, and skip
//! foreground startup work when `background_start_requested()` is true. The frontend
//! must inspect `background_status` before starting its own API/bootstrap work.

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
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct BackgroundStatus {
    pub preferences: BackgroundPreferences,
    pub background_start: bool,
    pub launch_at_login_supported: bool,
    /// Whether the requested entry is currently present. Does not claim OS policy allows it.
    pub login_entry_present: bool,
    pub call_detection_available: bool,
    pub limitation: &'static str,
}

struct BackgroundState {
    preferences: Mutex<BackgroundPreferences>,
    path: PathBuf,
    pause_item: CheckMenuItem<tauri::Wry>,
    background_start: AtomicBool,
    exiting: AtomicBool,
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
    if !is_background_arguments(args.iter().map(String::as_str)) {
        if let Err(error) = open(app) {
            tracing::warn!("Could not restore workspace: {error}");
        }
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
    if background_start_requested() {
        if let Some(main) = app.get_webview_window("main") {
            main.hide().map_err(|e| e.to_string())?;
        }
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
        call_detection_available: false,
        limitation: "Native call adapters are not yet verified. Background controls do not record audio. Login changes take effect at the next sign-in; OS policy may disable startup.",
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
            if current.launch_at_login != preferences.launch_at_login {
                if let Err(rollback) = set_launch_at_login(&app, current.launch_at_login) {
                    return Err(format!("{error}; login rollback failed: {rollback}"));
                }
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
        let preferences =
            BackgroundPreferences { keep_running: true, suggestions_paused: true, launch_at_login: false };
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
        let expected =
            BackgroundPreferences { keep_running: true, launch_at_login: false, suggestions_paused: true };
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

