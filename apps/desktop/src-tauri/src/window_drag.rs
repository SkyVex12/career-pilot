//! Start native move/resize loops without frontend geometry polling.

#[cfg(any(windows, test))]
fn cursor_parameter(x: i32, y: i32) -> isize {
    // WM_NCLBUTTONDOWN contains two signed 16-bit coordinates, not a POINTS pointer.
    ((x as u16 as u32) | ((y as u16 as u32) << 16)) as isize
}

#[cfg(any(windows, test))]
fn resize_hit(direction: &str) -> Result<u32, String> {
    // Win32 HTLEFT through HTBOTTOMRIGHT. Keep unsupported directions out of the move loop.
    match direction {
        "West" => Ok(10),
        "East" => Ok(11),
        "North" => Ok(12),
        "NorthWest" => Ok(13),
        "NorthEast" => Ok(14),
        "South" => Ok(15),
        "SouthWest" => Ok(16),
        "SouthEast" => Ok(17),
        _ => Err("Unknown window resize direction.".into()),
    }
}

#[cfg(windows)]
fn primary_pressed() -> bool {
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON, VK_RBUTTON};
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_SWAPBUTTON};
    unsafe {
        let primary = if GetSystemMetrics(SM_SWAPBUTTON) != 0 { VK_RBUTTON } else { VK_LBUTTON };
        GetAsyncKeyState(primary.0 as i32) < 0
    }
}

#[cfg(windows)]
async fn start_native_loop(window: tauri::Window, hit: u32) -> Result<(), String> {
    use windows::Win32::Foundation::{LPARAM, POINT, WPARAM};
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetCapture, ReleaseCapture};
    use windows::Win32::UI::WindowsAndMessaging::{
        GetCursorPos, HTCAPTION, PostMessageW, SendMessageW, WM_LBUTTONUP, WM_NCLBUTTONDOWN,
    };

    let (send, receive) = tokio::sync::oneshot::channel();
    let target = window.clone();
    window
        .run_on_main_thread(move || {
            let result = (|| -> Result<(), String> {
                // Recheck on the native thread: queued IPC must not resize a minimized,
                // compact, fullscreen, or maximized window, or start a loop after release.
                if !primary_pressed() || target.is_minimized().map_err(|e| e.to_string())? {
                    return Ok(());
                }
                if hit != HTCAPTION
                    && (!target.is_resizable().map_err(|e| e.to_string())?
                        || target.is_maximized().map_err(|e| e.to_string())?
                        || target.is_fullscreen().map_err(|e| e.to_string())?)
                {
                    return Ok(());
                }
                let hwnd = target.hwnd().map_err(|e| e.to_string())?;
                unsafe {
                    let mut point = POINT::default();
                    GetCursorPos(&mut point).map_err(|e| e.to_string())?;
                    if !GetCapture().is_invalid() {
                        ReleaseCapture().map_err(|e| e.to_string())?;
                    }
                    // Tao 0.37 passes a stack address here. Windows requires packed coordinates.
                    // Send synchronously so release cleanup happens after the native loop.
                    SendMessageW(
                        hwnd,
                        WM_NCLBUTTONDOWN,
                        Some(WPARAM(hit as usize)),
                        Some(LPARAM(cursor_parameter(point.x, point.y))),
                    );
                    PostMessageW(Some(hwnd), WM_LBUTTONUP, WPARAM(0), LPARAM(0))
                        .map_err(|e| e.to_string())?;
                }
                Ok(())
            })();
            let _ = send.send(result);
        })
        .map_err(|e| e.to_string())?;
    receive.await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn start_window_drag(window: tauri::Window) -> Result<(), String> {
    #[cfg(windows)]
    return start_native_loop(window, windows::Win32::UI::WindowsAndMessaging::HTCAPTION).await;
    #[cfg(not(windows))]
    window.start_dragging().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn start_window_resize(window: tauri::Window, direction: String) -> Result<(), String> {
    #[cfg(windows)]
    return start_native_loop(window, resize_hit(&direction)?).await;
    #[cfg(not(windows))]
    {
        // Tauri's ResizeDirection is not publicly re-exported. Let its native API
        // infer and deserialize that type; macOS/Linux keep the existing behavior.
        let direction =
            serde_json::from_value(serde_json::Value::String(direction)).map_err(|e| e.to_string())?;
        window.start_resize_dragging(direction).map_err(|e| e.to_string())
    }
}

// Wry's undecorated edge hit-test window also forwards a POINTS pointer to the
// parent. Normalize those messages at the app HWND so ordinary border drags and
// the explicit overlay grip both receive the same correct screen coordinates.
#[cfg(windows)]
unsafe extern "system" fn resize_frame_proc(
    hwnd: windows::Win32::Foundation::HWND,
    message: u32,
    wparam: windows::Win32::Foundation::WPARAM,
    lparam: windows::Win32::Foundation::LPARAM,
    subclass: usize,
    _data: usize,
) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::Foundation::{LPARAM, LRESULT, POINT};
    use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass};
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_STYLE, GetCursorPos, GetWindowLongPtrW, HTCAPTION, IsIconic, IsZoomed, WM_NCDESTROY,
        WM_NCLBUTTONDOWN, WS_SIZEBOX,
    };
    unsafe {
        if message == WM_NCDESTROY {
            let _ = RemoveWindowSubclass(hwnd, Some(resize_frame_proc), subclass);
        } else if message == WM_NCLBUTTONDOWN
            && (wparam.0 == HTCAPTION as usize || (10..=17).contains(&wparam.0))
        {
            let resizing = wparam.0 != HTCAPTION as usize;
            if !primary_pressed()
                || IsIconic(hwnd).as_bool()
                || (resizing
                    && (IsZoomed(hwnd).as_bool()
                        || GetWindowLongPtrW(hwnd, GWL_STYLE) as u32 & WS_SIZEBOX.0 == 0))
            {
                return LRESULT(0);
            }
            let mut point = POINT::default();
            if GetCursorPos(&mut point).is_err() {
                // Never forward the malformed pointer if the cursor is unavailable.
                return LRESULT(0);
            }
            return DefSubclassProc(hwnd, message, wparam, LPARAM(cursor_parameter(point.x, point.y)));
        }
        DefSubclassProc(hwnd, message, wparam, lparam)
    }
}

/// Called on the setup thread after both app windows exist. No geometry changes.
pub(crate) fn setup(app: &tauri::AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    {
        use tauri::Manager;
        use windows::Win32::UI::Shell::SetWindowSubclass;
        for name in ["main", "overlay"] {
            if let Some(window) = app.get_webview_window(name) {
                let hwnd = window.hwnd().map_err(|e| e.to_string())?;
                if !unsafe { SetWindowSubclass(hwnd, Some(resize_frame_proc), 0x52555a45, 0) }.as_bool() {
                    return Err(format!("Could not install native resize handling for {name}."));
                }
            }
        }
    }
    #[cfg(not(windows))]
    let _ = app;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cursor_coordinates_keep_their_signed_bits_on_all_monitors() {
        for (x, y) in [(120, 400), (-1920, 80), (100, -1080), (-500, -200), (32767, -32768)] {
            let packed = cursor_parameter(x, y) as u32;
            assert_eq!(packed as u16 as i16 as i32, x);
            assert_eq!((packed >> 16) as u16 as i16 as i32, y);
        }
    }

    #[test]
    fn all_eight_resize_directions_map_to_native_edges_not_the_caption() {
        let directions =
            ["West", "East", "North", "NorthWest", "NorthEast", "South", "SouthWest", "SouthEast"];
        for (index, direction) in directions.into_iter().enumerate() {
            assert_eq!(resize_hit(direction), Ok(10 + index as u32));
        }
        for invalid in ["", "Caption", "South East", "southEast"] {
            assert!(resize_hit(invalid).is_err());
        }
    }
}
