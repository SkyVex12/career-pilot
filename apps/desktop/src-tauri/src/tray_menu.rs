//! Compact themed tray popup: native Windows menus cannot color individual text labels.
use crate::Res;
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window,
    WindowEvent,
};
const LABEL: &str = "tray-menu";
const WIDTH: f64 = 208.0;
const HEIGHT: f64 = 82.0;
#[derive(Default)]
struct State {
    ready: AtomicBool,
    pending: Mutex<Option<PhysicalPosition<f64>>>,
}
pub(crate) fn setup(app: &AppHandle) {
    app.manage(State::default());
}

pub(crate) fn show(app: &AppHandle, point: PhysicalPosition<f64>) -> Res<()> {
    if crate::shutdown::busy(app) {
        return Ok(());
    }
    *app.state::<State>().pending.lock().unwrap() = Some(point);
    if app.get_webview_window(LABEL).is_none() {
        WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("index.html?tray-menu".into()))
            .title("CoRuby")
            .inner_size(WIDTH, HEIGHT)
            .decorations(false)
            .resizable(false)
            .minimizable(false)
            .maximizable(false)
            .skip_taskbar(true)
            .always_on_top(true)
            .visible(false)
            .focused(false)
            // This popup shares WebView2's profile with the main window. Different browser
            // arguments make Windows reject its creation before it can report readiness.
            .additional_browser_args(crate::BROWSER_ARGS)
            .background_throttling(tauri::utils::config::BackgroundThrottlingPolicy::Disabled)
            .build()
            .map_err(|e| e.to_string())?;
    }
    if app.state::<State>().ready.load(Ordering::SeqCst) {
        present(app)?;
    }
    Ok(())
}
fn present(app: &AppHandle) -> Res<()> {
    let point = app.state::<State>().pending.lock().unwrap().take();
    let Some(point) = point else {
        return Ok(());
    };
    let window = app.get_webview_window(LABEL).ok_or("Tray menu unavailable")?;
    let monitor = app
        .monitor_from_point(point.x, point.y)
        .map_err(|e| e.to_string())?
        .or(app.primary_monitor().map_err(|e| e.to_string())?)
        .ok_or("No display available")?;
    let scale = monitor.scale_factor();
    let size = ((WIDTH * scale).round() as i32, (HEIGHT * scale).round() as i32);
    let area = monitor.work_area();
    let position = menu_position(
        (point.x.round() as i32, point.y.round() as i32),
        (area.position.x, area.position.y, area.size.width as i32, area.size.height as i32),
        size,
    );
    window.set_size(tauri::PhysicalSize::new(size.0 as u32, size.1 as u32)).map_err(|e| e.to_string())?;
    window.set_position(PhysicalPosition::new(position.0, position.1)).map_err(|e| e.to_string())?;
    window
        .emit("copilot://tray-menu", crate::background::suggestions_paused(app))
        .map_err(|e| e.to_string())?;
    crate::stealth::show(&window)?;
    window.set_focus().map_err(|e| e.to_string())
}
fn menu_position(point: (i32, i32), area: (i32, i32, i32, i32), size: (i32, i32)) -> (i32, i32) {
    let (x, y, w, h) = area;
    let left = (point.0 - size.0 / 2).clamp(x, (x + w - size.0).max(x));
    let preferred_y = if point.1 - size.1 >= y { point.1 - size.1 } else { point.1 };
    (left, preferred_y.clamp(y, (y + h - size.1).max(y)))
}
fn check_window(window: &WebviewWindow) -> Res<()> {
    if window.label() == LABEL { Ok(()) } else { Err("This command belongs to the tray menu.".into()) }
}
#[tauri::command]
pub(crate) fn tray_menu_ready(app: AppHandle, window: WebviewWindow) -> Res<bool> {
    check_window(&window)?;
    app.state::<State>().ready.store(true, Ordering::SeqCst);
    present(&app)?;
    Ok(crate::background::suggestions_paused(&app))
}
#[tauri::command]
pub(crate) async fn tray_menu_action(app: AppHandle, window: WebviewWindow, action: String) -> Res<()> {
    check_window(&window)?;
    match action.as_str() {
        "pause" => {
            crate::background::toggle_pause(&app)?;
            window.hide().map_err(|e| e.to_string())
        }
        "exit" => {
            window.hide().map_err(|e| e.to_string())?;
            crate::shutdown::request(app);
            Ok(())
        }
        "dismiss" => window.hide().map_err(|e| e.to_string()),
        _ => Err("Unknown tray action".into()),
    }
}
pub(crate) fn on_window_event(window: &Window, event: &WindowEvent) -> bool {
    if window.label() != LABEL {
        return false;
    }
    match event {
        WindowEvent::Focused(false) => {
            let _ = window.hide();
        }
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            let _ = window.hide();
        }
        WindowEvent::Destroyed => {
            if let Some(state) = window.app_handle().try_state::<State>() {
                state.ready.store(false, Ordering::SeqCst);
            }
        }
        _ => {}
    }
    true
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn anchors_above_bottom_taskbar_and_stays_on_negative_origin_display() {
        assert_eq!(menu_position((1800, 1075), (0, 0, 1920, 1040), (208, 82)), (1696, 958));
        assert_eq!(menu_position((-1890, 1050), (-1920, 0, 1920, 1040), (312, 123)), (-1920, 917));
    }
    #[test]
    fn top_and_side_taskbars_do_not_push_the_menu_off_screen() {
        assert_eq!(menu_position((100, 10), (0, 40, 1920, 1040), (208, 82)), (0, 40));
        assert_eq!(menu_position((1915, 400), (0, 0, 1880, 1080), (208, 82)), (1672, 318));
    }
}
