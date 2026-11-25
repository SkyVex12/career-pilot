//! Main-window geometry and temporary consent overrides for the shared window preferences.

use std::sync::{Mutex, atomic::Ordering};

use tauri::{AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition, WebviewWindow};
use tauri_plugin_global_shortcut::Shortcut;

use crate::{AppState, OPEN_CALL_EVENT, OverlayPrefs, Res, hotkeys::Action, stealth};

// Apply once before the first presentation. Page changes and compact-mode transitions
// must never replace a size chosen by the user.
const STARTUP_MARGIN: f64 = 24.0;

fn initial_size(preferred: LogicalSize<f64>, available: LogicalSize<f64>) -> LogicalSize<f64> {
    let fit = |wanted: f64, room: f64, minimum: f64| {
        let inset = ((room - 2.0 * STARTUP_MARGIN) / 8.0).floor() * 8.0;
        wanted.min(inset.max(minimum.min(room)))
    };
    LogicalSize::new(
        fit(preferred.width, available.width, 400.0),
        fit(preferred.height, available.height, 240.0),
    )
}

pub(crate) fn fit_initial_window(window: &WebviewWindow) -> Res<()> {
    let monitor = window
        .current_monitor()
        .map_err(|e| e.to_string())?
        .or(window.primary_monitor().map_err(|e| e.to_string())?);
    let Some(monitor) = monitor else { return Ok(()) };
    let scale = monitor.scale_factor();
    let work = monitor.work_area();
    let preferred = window.inner_size().map_err(|e| e.to_string())?.to_logical::<f64>(scale);
    let size = initial_size(preferred, work.size.to_logical::<f64>(scale));
    // Even an unusually small work area must retain reachable window controls.
    window
        .set_min_size(Some(LogicalSize::new(400.0_f64.min(size.width), 240.0_f64.min(size.height))))
        .map_err(|e| e.to_string())?;
    window.set_size(size).map_err(|e| e.to_string())?;
    let physical = size.to_physical::<u32>(scale);
    window
        .set_position(PhysicalPosition::new(
            work.position.x + ((work.size.width.saturating_sub(physical.width)) / 2) as i32,
            work.position.y + ((work.size.height.saturating_sub(physical.height)) / 2) as i32,
        ))
        .map_err(|e| e.to_string())
}

pub(crate) const EVENT: &str = "copilot://workspace-window";

pub(crate) struct WorkspaceWindowState {
    control_prompt: Mutex<Option<ControlPrompt>>,
}

struct ControlPrompt {
    key: String,
    restore_on_top: bool,
}

impl Default for WorkspaceWindowState {
    fn default() -> Self {
        Self { control_prompt: Mutex::new(None) }
    }
}

fn require_workspace(window: &WebviewWindow) -> Res<()> {
    if window.label() != "main" {
        return Err("These controls belong to the main workspace window.".into());
    }
    Ok(())
}

/// This cache contains only shortcuts whose native registration succeeded. Never query
/// the plugin here: it holds its own registry lock while dispatching recovery hotkeys.
fn registered_recovery(shortcuts: &[(Shortcut, Action)]) -> Option<Shortcut> {
    shortcuts.iter().find(|(_, action)| *action == Action::ClickThroughOff).map(|(shortcut, _)| *shortcut)
}

pub(crate) fn recovery_shortcut(app: &AppHandle) -> Option<Shortcut> {
    let state = app.state::<AppState>();
    registered_recovery(&state.overlay.shortcuts.lock().unwrap())
}

fn prefs(window: &WebviewWindow) -> Res<OverlayPrefs> {
    require_workspace(window)?;
    Ok(crate::build_prefs(window.app_handle(), false))
}

pub(crate) fn emit_prefs(window: &WebviewWindow) -> Res<()> {
    window.emit_to(window.label(), EVENT, prefs(window)?).map_err(|error| error.to_string())
}

#[tauri::command]
pub(crate) fn window_prefs(window: WebviewWindow) -> Res<OverlayPrefs> {
    prefs(&window)
}

#[tauri::command]
pub(crate) fn set_window_opacity(window: WebviewWindow, value: f64) -> Res<()> {
    require_workspace(&window)?;
    crate::set_overlay_opacity(window.app_handle().clone(), value);
    Ok(())
}

#[tauri::command]
pub(crate) fn set_window_on_top(window: WebviewWindow, on: bool) -> Res<()> {
    require_workspace(&window)?;
    crate::set_overlay_on_top(window.app_handle().clone(), on)
}

/// Consent stays interactive and above other windows while preserving the shared choice.
pub(crate) fn apply_on_top(window: &WebviewWindow, on: bool) -> Res<()> {
    let state = window.state::<WorkspaceWindowState>();
    let prompt = {
        let mut active = state.control_prompt.lock().unwrap();
        if let Some(prompt) = active.as_mut() {
            prompt.restore_on_top = on;
        }
        active.is_some()
    };
    window.set_always_on_top(on || prompt).map_err(|error| error.to_string())
}

pub(crate) fn apply_click_through(window: &WebviewWindow, on: bool) -> Res<()> {
    let prompt = window.state::<WorkspaceWindowState>().control_prompt.lock().unwrap().is_some();
    window.set_ignore_cursor_events(effective_click_through(on, prompt)).map_err(|error| error.to_string())
}

/// Attempt both native windows even when one fails. Recovery must still reach the other.
pub(crate) fn update_both(
    previous: bool,
    next: bool,
    rollback: bool,
    mut apply: impl FnMut(&str, bool) -> Res<()>,
) -> Res<()> {
    let main = apply("main", next);
    let overlay = apply("overlay", next);
    let result = main.and(overlay);
    if result.is_err() && rollback {
        let _ = apply("main", previous);
        let _ = apply("overlay", previous);
    }
    result
}

fn effective_click_through(preference: bool, prompt: bool) -> bool {
    preference && !prompt
}

fn take_matching_prompt(active: &mut Option<ControlPrompt>, key: &str) -> Option<ControlPrompt> {
    if active.as_ref().map(|prompt| prompt.key.as_str()) != Some(key) {
        return None;
    }
    active.take()
}

fn finish_control_prompt(window: &WebviewWindow, key: &str) -> Res<()> {
    let state = window.state::<WorkspaceWindowState>();
    let previous = take_matching_prompt(&mut state.control_prompt.lock().unwrap(), key);
    if let Some(previous) = previous {
        // Restore both latest preferences, including a recovery hotkey used during the prompt.
        let clicks = window.set_ignore_cursor_events(
            window.state::<AppState>().overlay.click_through.load(Ordering::SeqCst),
        );
        let top = window.set_always_on_top(previous.restore_on_top);
        clicks.map_err(|e| e.to_string())?;
        top.map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Consent has one owner: the main workspace. Show it once per request even when all
/// windows are hidden, and temporarily accept clicks without rewriting the preference.
#[tauri::command]
pub(crate) async fn screen_control_prompt(
    window: WebviewWindow,
    session_id: uuid::Uuid,
    request_id: String,
    active: bool,
    duration_ms: u64,
) -> Res<()> {
    require_workspace(&window)?;
    if !(8..=80).contains(&request_id.len())
        || !request_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return Err("Invalid screen control request.".into());
    }
    if active && !(1..=60_000).contains(&duration_ms) {
        return Err("This screen control request has expired.".into());
    }
    let key = format!("{session_id}/{request_id}");
    let ui_key = key.clone();
    let app = window.app_handle().clone();
    let ui_app = app.clone();
    let (send, receive) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        let result = (|| -> Res<bool> {
            if !active {
                finish_control_prompt(&window, &ui_key)?;
                return Ok(false);
            }
            let state = ui_app.state::<AppState>();
            let same_session = state
                .engine
                .lock()
                .unwrap()
                .as_ref()
                .is_some_and(|engine| engine.session_id() == Some(session_id));
            if !same_session {
                return Err("The call for this control request has ended.".into());
            }
            let prefs = window.state::<WorkspaceWindowState>();
            let on_top = window.is_always_on_top().map_err(|e| e.to_string())?;
            {
                let mut prompt = prefs.control_prompt.lock().unwrap();
                if prompt.as_ref().map(|p| p.key.as_str()) == Some(ui_key.as_str()) {
                    return Ok(false);
                }
                let restore_on_top = prompt.as_ref().map(|p| p.restore_on_top).unwrap_or(on_top);
                *prompt = Some(ControlPrompt { key: ui_key.clone(), restore_on_top });
            }
            let shown = (|| -> Res<()> {
                window.set_ignore_cursor_events(false).map_err(|e| e.to_string())?;
                // The live overlay may itself be always on top. Consent must be above it.
                window.set_always_on_top(true).map_err(|e| e.to_string())?;
                // The existing handler restores compact bounds before opening Call.
                ui_app.emit_to("main", OPEN_CALL_EVENT, ()).map_err(|e| e.to_string())?;
                stealth::show(&window)?;
                if window.is_minimized().map_err(|e| e.to_string())? {
                    window.unminimize().map_err(|e| e.to_string())?;
                }
                window.set_focus().map_err(|e| e.to_string())?;
                Ok(())
            })();
            if let Err(error) = shown {
                let _ = finish_control_prompt(&window, &ui_key);
                return Err(error);
            }
            Ok(true)
        })();
        let _ = send.send(result);
    })
    .map_err(|e| e.to_string())?;
    let started = receive.await.map_err(|_| "Screen control prompt was interrupted.".to_string())??;
    if started {
        // A reloaded or crashed renderer cannot leave the temporary input override behind.
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(duration_ms)).await;
            let ui_app = app.clone();
            let _ = app.run_on_main_thread(move || {
                if let Some(window) = ui_app.get_webview_window("main") {
                    let _ = finish_control_prompt(&window, &key);
                }
            });
        });
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn set_window_click_through(window: WebviewWindow, on: bool) -> Res<()> {
    require_workspace(&window)?;
    crate::apply_click_through(window.app_handle(), on)
}

/// The existing global recovery key clears both the workspace and live overlay.
/// Also call this before shortcuts are re-registered, so a failed binding cannot strand it.
pub(crate) fn clear_click_through(app: &AppHandle) -> Res<()> {
    let Some(state) = app.try_state::<AppState>() else { return Ok(()) };
    if !state.overlay.click_through.load(Ordering::SeqCst) {
        return Ok(());
    }
    crate::apply_click_through(app, false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    #[test]
    fn shared_flags_apply_to_both_windows_and_roll_back_a_failed_change() {
        let mut values = [false, false];
        update_both(false, true, true, |label, on| {
            values[usize::from(label == "overlay")] = on;
            Ok(())
        })
        .unwrap();
        assert_eq!(values, [true, true]);
        let result = update_both(true, false, true, |label, on| {
            if label == "overlay" && !on {
                return Err("native failure".into());
            }
            values[usize::from(label == "overlay")] = on;
            Ok(())
        });
        assert!(result.is_err());
        assert_eq!(values, [true, true]);
    }

    #[test]
    fn click_through_recovery_still_reaches_overlay_when_main_fails() {
        let mut reached_overlay = false;
        let result = update_both(true, false, false, |label, on| {
            assert!(!on);
            if label == "main" {
                return Err("main unavailable".into());
            }
            reached_overlay = true;
            Ok(())
        });
        assert!(result.is_err());
        assert!(reached_overlay);
    }

    #[test]
    fn startup_size_is_bounded_by_the_usable_display_at_any_scale() {
        let preferred = LogicalSize::new(800.0, 672.0);
        for (physical_width, physical_height, scale, width, height) in [
            (1920, 1040, 1.0, 800.0, 672.0),
            (1366, 728, 1.0, 800.0, 672.0),
            (1920, 1040, 1.5, 800.0, 640.0),
            (1920, 1040, 2.0, 800.0, 472.0),
            (800, 600, 1.0, 752.0, 552.0),
            (320, 200, 1.0, 320.0, 200.0),
        ] {
            let available = tauri::PhysicalSize::new(physical_width, physical_height).to_logical(scale);
            assert_eq!(initial_size(preferred, available), LogicalSize::new(width, height));
        }
    }

    #[test]
    fn startup_fitting_never_enlarges_the_configured_window() {
        let preferred = LogicalSize::new(640.0, 480.0);
        assert_eq!(initial_size(preferred, LogicalSize::new(1920.0, 1040.0)), preferred);
    }

    #[test]
    fn consent_temporarily_overrides_click_through_without_rewriting_it() {
        assert!(effective_click_through(true, false));
        assert!(!effective_click_through(true, true));
        assert!(!effective_click_through(false, true));
        assert!(!effective_click_through(false, false));
    }

    #[test]
    fn an_old_prompt_cleanup_cannot_end_a_new_prompt() {
        let mut current =
            Some(ControlPrompt { key: "session/new-request".to_string(), restore_on_top: false });
        assert!(take_matching_prompt(&mut current, "session/old-request").is_none());
        assert!(current.is_some());
        let previous = take_matching_prompt(&mut current, "session/new-request").unwrap();
        assert!(!previous.restore_on_top);
        assert!(current.is_none());
        assert!(take_matching_prompt(&mut current, "session/new-request").is_none());
    }

    #[test]
    fn recovery_requires_a_successfully_registered_off_action() {
        let off = Shortcut::from_str("Control+Shift+G").unwrap();
        let toggle = Shortcut::from_str("Control+Shift+T").unwrap();
        let shortcuts = [(off, Action::ClickThroughOff), (toggle, Action::ClickThrough)];
        assert_eq!(registered_recovery(&shortcuts), Some(off));
        assert_eq!(registered_recovery(&[(toggle, Action::ClickThrough)]), None);
        assert_eq!(registered_recovery(&[(off, Action::ClickThrough)]), None);
        assert_eq!(registered_recovery(&[]), None);
    }
}
