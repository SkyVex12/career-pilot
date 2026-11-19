//! Start the operating system's move loop without a frontend position/resize loop.

#[cfg(any(windows, test))]
fn cursor_parameter(x: i32, y: i32) -> isize {
    // WM_NCLBUTTONDOWN contains two signed 16-bit coordinates, not a POINTS pointer.
    ((x as u16 as u32) | ((y as u16 as u32) << 16)) as isize
}

#[tauri::command]
pub async fn start_window_drag(window: tauri::WebviewWindow) -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::{LPARAM, POINT, WPARAM};
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            GetAsyncKeyState, GetCapture, ReleaseCapture, VK_LBUTTON, VK_RBUTTON,
        };
        use windows::Win32::UI::WindowsAndMessaging::{
            GetCursorPos, GetSystemMetrics, HTCAPTION, PostMessageW, SM_SWAPBUTTON, SendMessageW,
            WM_LBUTTONUP, WM_NCLBUTTONDOWN,
        };

        let (send, receive) = tokio::sync::oneshot::channel();
        let target = window.clone();
        window
            .run_on_main_thread(move || {
                let result = (|| -> Result<(), String> {
                    let hwnd = target.hwnd().map_err(|e| e.to_string())?;
                    unsafe {
                        // A delayed IPC after release must never leave the window following the mouse.
                        let primary =
                            if GetSystemMetrics(SM_SWAPBUTTON) != 0 { VK_RBUTTON } else { VK_LBUTTON };
                        let pressed = GetAsyncKeyState(primary.0 as i32) < 0;
                        tracing::info!("window drag requested: primary pressed={pressed}");
                        if !pressed {
                            return Ok(());
                        }
                        let mut point = POINT::default();
                        GetCursorPos(&mut point).map_err(|e| e.to_string())?;
                        tracing::info!(
                            "window drag cursor={},{} capture={:?}",
                            point.x,
                            point.y,
                            GetCapture()
                        );
                        if !GetCapture().is_invalid() {
                            ReleaseCapture().map_err(|e| e.to_string())?;
                        }
                        // Tao 0.37 passes a stack address here. Windows requires packed coordinates.
                        // Send synchronously so release cleanup happens after the native move loop.
                        SendMessageW(
                            hwnd,
                            WM_NCLBUTTONDOWN,
                            Some(WPARAM(HTCAPTION as usize)),
                            Some(LPARAM(cursor_parameter(point.x, point.y))),
                        );
                        tracing::info!("window drag ended at {:?}", target.outer_position());
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
    #[cfg(not(windows))]
    window.start_dragging().map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cursor_coordinates_keep_their_signed_bits_on_all_monitors() {
        for (x, y) in [(120, 400), (-1920, 80), (100, -1080), (-500, -200)] {
            let packed = cursor_parameter(x, y) as u32;
            assert_eq!(packed as u16 as i16 as i32, x);
            assert_eq!((packed >> 16) as u16 as i16 as i32, y);
        }
    }
}
