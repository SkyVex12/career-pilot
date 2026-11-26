//! Keep the single-instance IPC window out of screen-sharing source lists.
//!
//! tauri-plugin-single-instance 2.5.5 marks its layered helper visible, so Chromium's GDI
//! picker can show a named black tile even though it has no UI. Hide only that helper after
//! plugin setup. Its class/title must stay intact: second launches use both in FindWindowW
//! before sending WM_COPYDATA, which works with a hidden window. Tao's separate event target
//! does need its native visibility and must not be changed.

use windows::Win32::UI::WindowsAndMessaging::{
    FindWindowW, GetWindowThreadProcessId, IsWindowVisible, SWP_HIDEWINDOW, SWP_NOACTIVATE, SWP_NOMOVE,
    SWP_NOSIZE, SWP_NOZORDER, SetWindowPos,
};
use windows::core::PCWSTR;

/// Register immediately after the single-instance plugin, before WebView creation.
pub(crate) fn init<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri::plugin::Builder::new("native-helpers")
        .setup(|app, _api| {
            if let Err(error) = hide_single_instance(&app.config().identifier) {
                tracing::warn!(%error, "Could not hide the single-instance helper");
            }
            Ok(())
        })
        .build()
}

/// Call on the app's UI thread after plugin initialization, regardless of the Stealth setting.
pub(crate) fn hide_single_instance(identifier: &str) -> Result<(), String> {
    let class: Vec<u16> = format!("{identifier}-sic").encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = format!("{identifier}-siw").encode_utf16().chain(Some(0)).collect();
    unsafe {
        let window = FindWindowW(PCWSTR(class.as_ptr()), PCWSTR(title.as_ptr()))
            .map_err(|error| format!("Single-instance helper was not found: {error}"))?;
        let mut process_id = 0;
        GetWindowThreadProcessId(window, Some(&mut process_id));
        if process_id != std::process::id() {
            return Err("Single-instance helper belongs to another process.".into());
        }
        SetWindowPos(
            window,
            None,
            0,
            0,
            0,
            0,
            SWP_HIDEWINDOW | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER,
        )
        .map_err(|error| format!("Could not hide the single-instance helper: {error}"))?;
        if IsWindowVisible(window).as_bool() {
            return Err("Single-instance helper is still visible.".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
