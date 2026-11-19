//! Session-only presentation preferences for the main workspace window.

use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

use tauri::{AppHandle, Emitter, Manager, WebviewWindow};
use tauri_plugin_global_shortcut::Shortcut;

use crate::{AppState, OverlayHotkeys, OverlayPrefs, Res, hotkeys::Action, layout};

pub(crate) const EVENT: &str = "copilot://workspace-window";

pub(crate) struct WorkspaceWindowState {
    opacity: Mutex<f64>,
    click_through: AtomicBool,
}

impl Default for WorkspaceWindowState {
    fn default() -> Self {
        Self { opacity: Mutex::new(1.0), click_through: AtomicBool::new(false) }
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
    Ok(OverlayPrefs {
        opacity,
        compact: false,
        click_through,
        on_top: window.is_always_on_top().map_err(|error| error.to_string())?,
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
    window.set_always_on_top(on).map_err(|error| error.to_string())?;
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
    window.set_ignore_cursor_events(on).map_err(|error| error.to_string())?;
    state.click_through.store(on, Ordering::SeqCst);
    emit_prefs(window)
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
