//! Session-only presentation preferences for the main workspace window.

use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

use tauri::{AppHandle, Emitter, Manager, WebviewWindow};
use tauri_plugin_global_shortcut::Shortcut;

use crate::{AppState, OPEN_CALL_EVENT, OverlayHotkeys, OverlayPrefs, Res, hotkeys::Action, layout, stealth};

pub(crate) const EVENT: &str = "copilot://workspace-window";

pub(crate) struct WorkspaceWindowState {
    opacity: Mutex<f64>,
    click_through: AtomicBool,
    control_prompt: Mutex<Option<ControlPrompt>>,
}

struct ControlPrompt {
    key: String,
    restore_on_top: bool,
}

impl Default for WorkspaceWindowState {
    fn default() -> Self {
        Self {
            opacity: Mutex::new(1.0),
            click_through: AtomicBool::new(false),
            control_prompt: Mutex::new(None),
        }
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

fn recovery_shortcut(app: &AppHandle) -> Option<Shortcut> {
    let state = app.state::<AppState>();
    registered_recovery(&state.overlay.shortcuts.lock().unwrap())
}

fn prefs(window: &WebviewWindow) -> Res<OverlayPrefs> {
    require_workspace(window)?;
    let state = window.state::<WorkspaceWindowState>();
    let opacity = *state.opacity.lock().unwrap();
    let click_through = state.click_through.load(Ordering::SeqCst);
    let saved_on_top = state.control_prompt.lock().unwrap().as_ref().map(|prompt| prompt.restore_on_top);
    let on_top = match saved_on_top {
        Some(value) => value,
        None => window.is_always_on_top().map_err(|error| error.to_string())?,
    };
    Ok(OverlayPrefs {
        opacity,
        compact: false,
        click_through,
        on_top,
        // The other shortcuts still control the separate live overlay.
        hotkeys: OverlayHotkeys {
            toggle_overlay: String::new(),
            click_through: String::new(),
            click_through_off: recovery_shortcut(window.app_handle())
                .map(|shortcut| shortcut.to_string())
                .unwrap_or_default(),
            opacity_up: String::new(),
            opacity_down: String::new(),
        },
    })
}

fn emit_prefs(window: &WebviewWindow) -> Res<()> {
    window.emit_to(window.label(), EVENT, prefs(window)?).map_err(|error| error.to_string())
}

#[tauri::command]
pub(crate) fn window_prefs(window: WebviewWindow) -> Res<OverlayPrefs> {
    prefs(&window)
}

#[tauri::command]
pub(crate) fn set_window_opacity(window: WebviewWindow, value: f64) -> Res<()> {
    require_workspace(&window)?;
    *window.state::<WorkspaceWindowState>().opacity.lock().unwrap() = layout::clamp_opacity(value);
    // The transparent WebView paints its surfaces using this alpha; text remains opaque.
    emit_prefs(&window)
}

#[tauri::command]
pub(crate) fn set_window_on_top(window: WebviewWindow, on: bool) -> Res<()> {
    require_workspace(&window)?;
    let state = window.state::<WorkspaceWindowState>();
    let prompt = {
        let mut active = state.control_prompt.lock().unwrap();
        if let Some(prompt) = active.as_mut() {
            prompt.restore_on_top = on;
        }
        active.is_some()
    };
    window.set_always_on_top(on || prompt).map_err(|error| error.to_string())?;
    emit_prefs(&window)
}

fn apply_click_through(window: &WebviewWindow, on: bool) -> Res<()> {
    require_workspace(window)?;
    if on && recovery_shortcut(window.app_handle()).is_none() {
        return Err(
            "Click-through needs a working recovery shortcut. Set a Click-through off key in Settings first."
                .into(),
        );
    }
    let state = window.state::<WorkspaceWindowState>();
    // Never hold application locks while calling into the native event loop.
    let prompt = state.control_prompt.lock().unwrap().is_some();
    window
        .set_ignore_cursor_events(effective_click_through(on, prompt))
        .map_err(|error| error.to_string())?;
    state.click_through.store(on, Ordering::SeqCst);
    emit_prefs(window)
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
        let clicks = window.set_ignore_cursor_events(state.click_through.load(Ordering::SeqCst));
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
    apply_click_through(&window, on)
}

/// The existing global recovery key clears both the workspace and live overlay.
/// Also call this before shortcuts are re-registered, so a failed binding cannot strand it.
pub(crate) fn clear_click_through(app: &AppHandle) -> Res<()> {
    let Some(state) = app.try_state::<WorkspaceWindowState>() else { return Ok(()) };
    if !state.click_through.load(Ordering::SeqCst) {
        return Ok(());
    }
    let window = app.get_webview_window("main").ok_or("No main workspace window.")?;
    apply_click_through(&window, false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

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
