//! Keep the Windows compact WebView stable while changing its visible and interactive shape.

use tauri::WebviewWindow;

use crate::Res;

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum CompactShape {
    Full,
    Toolbar,
    Dot,
}

#[cfg(windows)]
static DOT_REGION: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

#[cfg(windows)]
const COMPACT_FRAME_SUBCLASS: usize = 0x4350_4652;

#[cfg(windows)]
const COMPACT_FRAME_PROPERTY: windows_core::PCWSTR = windows_core::w!("CareerPilot.CompactFrame");

#[cfg(windows)]
const FRAME_STYLE: u32 = {
    use windows::Win32::UI::WindowsAndMessaging::{WS_CAPTION, WS_THICKFRAME};
    WS_CAPTION.0 | WS_THICKFRAME.0
};

#[cfg(windows)]
const FRAME_EX_STYLE: u32 = {
    use windows::Win32::UI::WindowsAndMessaging::{
        WS_EX_CLIENTEDGE, WS_EX_DLGMODALFRAME, WS_EX_STATICEDGE, WS_EX_WINDOWEDGE,
    };
    WS_EX_CLIENTEDGE.0 | WS_EX_DLGMODALFRAME.0 | WS_EX_STATICEDGE.0 | WS_EX_WINDOWEDGE.0
};

#[cfg(windows)]
struct CompactFrame {
    style: u32,
    ex_style: u32,
    active: bool,
}

/// Tao retains native caption styles on undecorated windows, and regenerates
/// them when settings change. DWM can paint those styles even if WM_NCPAINT is
/// suppressed. Keep the compact HWND frameless without disabling its blur-based
/// transparency; activation events still flow through Tao.
#[cfg(windows)]
unsafe extern "system" fn compact_frame_proc(
    hwnd: windows::Win32::Foundation::HWND,
    message: u32,
    wparam: windows::Win32::Foundation::WPARAM,
    lparam: windows::Win32::Foundation::LPARAM,
    subclass: usize,
    data: usize,
) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::Foundation::{LPARAM, LRESULT};
    use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass};
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_EXSTYLE, GWL_STYLE, IsIconic, RemovePropW, STYLESTRUCT, WM_NCACTIVATE, WM_NCDESTROY, WM_NCPAINT,
        WM_STYLECHANGING,
    };

    unsafe {
        if message == WM_NCDESTROY {
            let _ = RemoveWindowSubclass(hwnd, Some(compact_frame_proc), subclass);
            let _ = RemovePropW(hwnd, COMPACT_FRAME_PROPERTY);
            let result = DefSubclassProc(hwnd, message, wparam, lparam);
            drop(Box::from_raw(data as *mut CompactFrame));
            return result;
        } else if (*(data as *const CompactFrame)).active && message == WM_STYLECHANGING {
            let result = DefSubclassProc(hwnd, message, wparam, lparam);
            // Modify the proposed style in place, without calling a setter from
            // its own notification or holding a Rust borrow across reentry.
            if lparam.0 != 0 {
                let style = &mut *(lparam.0 as *mut STYLESTRUCT);
                if wparam.0 as i32 == GWL_STYLE.0 {
                    style.styleNew &= !FRAME_STYLE;
                } else if wparam.0 as i32 == GWL_EXSTYLE.0 {
                    style.styleNew &= !FRAME_EX_STYLE;
                }
            }
            return result;
        } else if (*(data as *const CompactFrame)).active
            && matches!(message, WM_NCPAINT | WM_NCACTIVATE)
            && !IsIconic(hwnd).as_bool()
        {
            match message {
                WM_NCPAINT => return LRESULT(0),
                // -1 is the documented way to preserve activation processing
                // without asking DefWindowProc to repaint the native caption.
                WM_NCACTIVATE => return DefSubclassProc(hwnd, message, wparam, LPARAM(-1)),
                _ => {}
            }
        }
        DefSubclassProc(hwnd, message, wparam, lparam)
    }
}

#[cfg(windows)]
fn set_native_style(
    hwnd: windows::Win32::Foundation::HWND,
    index: windows::Win32::UI::WindowsAndMessaging::WINDOW_LONG_PTR_INDEX,
    style: u32,
) -> Res<()> {
    use windows::Win32::Foundation::{GetLastError, SetLastError, WIN32_ERROR};
    use windows::Win32::UI::WindowsAndMessaging::SetWindowLongPtrW;

    unsafe {
        SetLastError(WIN32_ERROR(0));
        if SetWindowLongPtrW(hwnd, index, style as isize) == 0 && GetLastError().0 != 0 {
            return Err(windows_core::Error::from_thread().to_string());
        }
    }
    Ok(())
}

#[cfg(windows)]
fn change_native_frame(hwnd: windows::Win32::Foundation::HWND, style: u32, ex_style: u32) -> Res<()> {
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_EXSTYLE, GWL_STYLE, GetWindowLongPtrW, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
        SWP_NOZORDER, SetWindowPos,
    };

    unsafe {
        let current = GetWindowLongPtrW(hwnd, GWL_STYLE) as u32;
        let current_ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32;
        let next = (current & !FRAME_STYLE) | style;
        let next_ex = (current_ex & !FRAME_EX_STYLE) | ex_style;
        if current == next && current_ex == next_ex {
            return Ok(());
        }
        if current != next {
            set_native_style(hwnd, GWL_STYLE, next)?;
        }
        if current_ex != next_ex {
            set_native_style(hwnd, GWL_EXSTYLE, next_ex)?;
        }
        // Refresh only when entering/leaving compact mode, never during the
        // dot/toolbar morph. Outer bounds, z-order and activation stay unchanged.
        SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
        )
        .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[cfg(windows)]
fn restore_compact_frame(hwnd: windows::Win32::Foundation::HWND) -> Res<()> {
    use windows::Win32::UI::Shell::RemoveWindowSubclass;
    use windows::Win32::UI::WindowsAndMessaging::{GetPropW, RemovePropW};

    unsafe {
        let frame = GetPropW(hwnd, COMPACT_FRAME_PROPERTY).0 as *mut CompactFrame;
        if frame.is_null() {
            return Ok(());
        }
        (*frame).active = false;
        if let Err(error) = change_native_frame(hwnd, (*frame).style, (*frame).ex_style) {
            (*frame).active = true;
            let _ = change_native_frame(hwnd, 0, 0);
            return Err(error);
        }
        if !RemoveWindowSubclass(hwnd, Some(compact_frame_proc), COMPACT_FRAME_SUBCLASS).as_bool() {
            (*frame).active = true;
            let _ = change_native_frame(hwnd, 0, 0);
            return Err("Could not restore the workspace window frame.".into());
        }
        let _ = RemovePropW(hwnd, COMPACT_FRAME_PROPERTY);
        drop(Box::from_raw(frame));
    }
    Ok(())
}

#[cfg(windows)]
fn guard_compact_frame(hwnd: windows::Win32::Foundation::HWND) -> Res<()> {
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::UI::Shell::SetWindowSubclass;
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_EXSTYLE, GWL_STYLE, GetPropW, GetWindowLongPtrW, RemovePropW, SetPropW,
    };

    unsafe {
        if !GetPropW(hwnd, COMPACT_FRAME_PROPERTY).0.is_null() {
            return Ok(());
        }
        let frame = Box::into_raw(Box::new(CompactFrame {
            style: GetWindowLongPtrW(hwnd, GWL_STYLE) as u32 & FRAME_STYLE,
            ex_style: GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32 & FRAME_EX_STYLE,
            active: true,
        }));
        if let Err(error) = SetPropW(hwnd, COMPACT_FRAME_PROPERTY, Some(HANDLE(frame.cast()))) {
            drop(Box::from_raw(frame));
            return Err(error.to_string());
        }
        if !SetWindowSubclass(hwnd, Some(compact_frame_proc), COMPACT_FRAME_SUBCLASS, frame as usize)
            .as_bool()
        {
            let _ = RemovePropW(hwnd, COMPACT_FRAME_PROPERTY);
            drop(Box::from_raw(frame));
            return Err("Could not protect the compact window's custom frame.".into());
        }
        if let Err(error) = change_native_frame(hwnd, 0, 0) {
            let _ = restore_compact_frame(hwnd);
            return Err(error);
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Bounds {
    x: i32,
    y: i32,
    width: i32,
    height: i32,
}

/// The visible Small launcher is 64 by 44 logical pixels. Its native rounded
/// rectangle includes a two-pixel antialias margin and stays centered in the toolbar.
#[cfg(any(windows, test))]
fn dot_region(client: Bounds, scale: f64) -> Res<Bounds> {
    if !scale.is_finite() || scale <= 0.0 || scale > 16.0 {
        return Err("Invalid window display scale.".into());
    }
    let width = (68.0 * scale).round() as i32;
    let height = (48.0 * scale).round() as i32;
    if width <= 0 || height <= 0 || client.width < width || client.height < height
        || client.width > 65_536 || client.height > 65_536 {
        return Err("Invalid compact surface dimensions.".into());
    }
    let dx = (f64::from(client.width - width) / 2.0).round() as i32;
    let dy = (f64::from(client.height - height) / 2.0).round() as i32;
    let x = client.x.checked_add(dx).ok_or("Window region is out of range.")?;
    let y = client.y.checked_add(dy).ok_or("Window region is out of range.")?;
    x.checked_add(width).ok_or("Window region is out of range.")?;
    y.checked_add(height).ok_or("Window region is out of range.")?;
    Ok(Bounds { x, y, width, height })
}

#[cfg(windows)]
fn apply_shape(hwnd: windows::Win32::Foundation::HWND, shape: CompactShape, refresh: bool) -> Res<()> {
    use std::sync::atomic::Ordering;
    use windows::Win32::Foundation::{POINT, RECT};
    use windows::Win32::Graphics::Gdi::{ClientToScreen, CreateRoundRectRgn, DeleteObject, SetWindowRgn};
    use windows::Win32::UI::HiDpi::GetDpiForWindow;
    use windows::Win32::UI::WindowsAndMessaging::{GetClientRect, GetWindowRect, IsIconic};

    // Called only on the native UI thread. Never hold a mutex across Win32 calls,
    // which may synchronously dispatch further window events.
    unsafe {
        if IsIconic(hwnd).as_bool() {
            return Err("Restore the window before changing its shape.".into());
        }
        let dot = shape == CompactShape::Dot;
        if shape != CompactShape::Full {
            // Install before changing the region, which can synchronously ask
            // Windows to paint its non-client area.
            guard_compact_frame(hwnd)?;
        }
        let previous_dot = DOT_REGION.load(Ordering::Relaxed);
        let update_region = refresh || previous_dot != dot;
        if update_region && dot {
            let mut client = RECT::default();
            let mut outer = RECT::default();
            let mut origin = POINT::default();
            GetClientRect(hwnd, &mut client).map_err(|error| error.to_string())?;
            GetWindowRect(hwnd, &mut outer).map_err(|error| error.to_string())?;
            if !ClientToScreen(hwnd, &mut origin).as_bool() {
                return Err(windows_core::Error::from_thread().to_string());
            }
            let region = dot_region(
                Bounds {
                    x: origin.x.checked_sub(outer.left).ok_or("Invalid client origin.")?,
                    y: origin.y.checked_sub(outer.top).ok_or("Invalid client origin.")?,
                    width: client.right.checked_sub(client.left).ok_or("Invalid client width.")?,
                    height: client.bottom.checked_sub(client.top).ok_or("Invalid client height.")?,
                },
                f64::from(GetDpiForWindow(hwnd)) / 96.0,
            )?;
            let handle =
                CreateRoundRectRgn(region.x, region.y, region.x + region.width, region.y + region.height,
                    (20.0 * f64::from(GetDpiForWindow(hwnd)) / 96.0).round() as i32,
                    (20.0 * f64::from(GetDpiForWindow(hwnd)) / 96.0).round() as i32);
            if handle.is_invalid() {
                return Err("Could not create the compact window region.".into());
            }
            if SetWindowRgn(hwnd, Some(handle), true) == 0 {
                let error = windows_core::Error::from_thread().to_string();
                // Ownership transfers only on success. Windows deletes an adopted
                // region itself when another shape replaces it or the HWND closes.
                let _ = DeleteObject(handle.into());
                return Err(format!("Could not apply the compact window region: {error}"));
            }
        } else if update_region && SetWindowRgn(hwnd, None, true) == 0 {
            return Err(format!(
                "Could not restore the workspace window region: {}",
                windows_core::Error::from_thread()
            ));
        }
        DOT_REGION.store(dot, Ordering::Relaxed);
        if shape == CompactShape::Full {
            // Keep the guard through the region clear and release it only after
            // success, including a toolbar -> full transition with no dot region.
            if let Err(error) = restore_compact_frame(hwnd) {
                // The frontend keeps its dot view when restoration fails. Put
                // its input region back too; a Dot retry never restores a frame.
                if previous_dot && let Err(rollback) = apply_shape(hwnd, CompactShape::Dot, true) {
                    tracing::warn!(%rollback, "Could not restore compact region after frame failure");
                }
                return Err(error);
            }
        }
    }
    Ok(())
}

/// Windows keeps the toolbar-sized HWND and WebView throughout the CSS morph.
/// The padded native region preserves the rounded launcher's antialiased edge while
/// letting the rest of the transparent toolbar pass through to other apps, without
/// a compositor surface resize at the animation's endpoint.
#[tauri::command]
pub(crate) async fn set_compact_window_shape(window: WebviewWindow, shape: CompactShape) -> Res<bool> {
    if window.label() != "main" {
        return Err("Compact presentation belongs to the main workspace window.".into());
    }

    #[cfg(windows)]
    {
        let (send, receive) = tokio::sync::oneshot::channel();
        let target = window.clone();
        window
            .run_on_main_thread(move || {
                let result = target
                    .hwnd()
                    .map_err(|error| error.to_string())
                    .and_then(|hwnd| apply_shape(hwnd, shape, false))
                    .map(|()| true);
                let _ = send.send(result);
            })
            .map_err(|error| error.to_string())?;
        receive.await.map_err(|error| error.to_string())?
    }

    #[cfg(not(windows))]
    {
        let _ = shape;
        Ok(false)
    }
}

pub(crate) fn on_window_event(window: &tauri::Window, event: &tauri::WindowEvent) {
    #[cfg(windows)]
    if window.label() == "main"
        && matches!(event, tauri::WindowEvent::ScaleFactorChanged { .. })
        && DOT_REGION.load(std::sync::atomic::Ordering::Relaxed)
    {
        let window = window.clone();
        // Calling run_on_main_thread directly here executes inline, before Tao
        // finishes its DPI bounds update. Queue from the runtime so geometry is
        // read after that event; do not subscribe to the region's own size events.
        tauri::async_runtime::spawn(async move {
            let target = window.clone();
            let _ = window.run_on_main_thread(move || {
                if DOT_REGION.load(std::sync::atomic::Ordering::Relaxed) {
                    let result = target
                        .hwnd()
                        .map_err(|error| error.to_string())
                        .and_then(|hwnd| apply_shape(hwnd, CompactShape::Dot, true));
                    if let Err(error) = result {
                        tracing::debug!(%error, "Could not refresh compact region after display scaling");
                    }
                }
            });
        });
    }

    #[cfg(not(windows))]
    let _ = (window, event);
}

fn validate_target(width: f64, height: f64) -> Res<()> {
    if !width.is_finite() || !height.is_finite() || !matches!((width, height), (280.0, 56.0) | (64.0, 44.0)) {
        return Err("Unsupported compact window size.".into());
    }
    Ok(())
}

fn centered_bounds(current: Bounds, width: f64, height: f64, scale: f64) -> Res<Bounds> {
    validate_target(width, height)?;
    if !scale.is_finite() || scale <= 0.0 || scale > 16.0 {
        return Err("Invalid window display scale.".into());
    }
    if current.width <= 0 || current.height <= 0 || current.width > 65_536 || current.height > 65_536 {
        return Err("Invalid current window bounds.".into());
    }
    let width = (width * scale).round() as i32;
    let height = (height * scale).round() as i32;
    if width == 0 || height == 0 {
        return Err("Invalid compact window dimensions.".into());
    }
    // Round the delta, not the absolute screen coordinate. Opposite transitions
    // then cancel even when a fractional scale requires half-pixel rounding.
    let dx = (f64::from(current.width - width) / 2.0).round() as i32;
    let dy = (f64::from(current.height - height) / 2.0).round() as i32;
    let x = current.x.checked_add(dx).ok_or("Window position is out of range.")?;
    let y = current.y.checked_add(dy).ok_or("Window position is out of range.")?;
    x.checked_add(width).ok_or("Window position is out of range.")?;
    y.checked_add(height).ok_or("Window position is out of range.")?;
    Ok(Bounds { x, y, width, height })
}

#[tauri::command]
pub(crate) async fn resize_compact_window(window: WebviewWindow, width: f64, height: f64) -> Res<()> {
    if window.label() != "main" {
        return Err("Compact presentation belongs to the main workspace window.".into());
    }
    validate_target(width, height)?;

    #[cfg(windows)]
    {
        use windows::Win32::Foundation::RECT;
        use windows::Win32::UI::HiDpi::GetDpiForWindow;
        use windows::Win32::UI::WindowsAndMessaging::{
            GetWindowRect, IsIconic, SWP_NOACTIVATE, SWP_NOZORDER, SetWindowPos,
        };

        let (send, receive) = tokio::sync::oneshot::channel();
        let target = window.clone();
        window
            .run_on_main_thread(move || {
                let result = (|| -> Res<()> {
                    let hwnd = target.hwnd().map_err(|error| error.to_string())?;
                    // Read geometry and DPI in the same UI-thread turn as the write.
                    // SetWindowPos updates both bounds atomically without activating.
                    unsafe {
                        if IsIconic(hwnd).as_bool() {
                            return Err("Restore the window before changing its size.".into());
                        }
                        let mut rect = RECT::default();
                        GetWindowRect(hwnd, &mut rect).map_err(|error| error.to_string())?;
                        let current = Bounds {
                            x: rect.left,
                            y: rect.top,
                            width: rect.right.checked_sub(rect.left).ok_or("Invalid window width.")?,
                            height: rect.bottom.checked_sub(rect.top).ok_or("Invalid window height.")?,
                        };
                        let next =
                            centered_bounds(current, width, height, f64::from(GetDpiForWindow(hwnd)) / 96.0)?;
                        if next == current {
                            return Ok(());
                        }
                        SetWindowPos(
                            hwnd,
                            None,
                            next.x,
                            next.y,
                            next.width,
                            next.height,
                            SWP_NOZORDER | SWP_NOACTIVATE,
                        )
                        .map_err(|error| error.to_string())?;
                    }
                    Ok(())
                })();
                let _ = send.send(result);
            })
            .map_err(|error| error.to_string())?;
        receive.await.map_err(|error| error.to_string())?
    }

    #[cfg(not(windows))]
    {
        // Tauri dispatches these calls to the platform event loop. Keep their
        // synchronous getters off that loop so it can service the requests.
        tauri::async_runtime::spawn_blocking(move || -> Res<()> {
            if window.is_minimized().map_err(|error| error.to_string())? {
                return Err("Restore the window before changing its size.".into());
            }
            let position = window.outer_position().map_err(|error| error.to_string())?;
            let size = window.outer_size().map_err(|error| error.to_string())?;
            let scale = window.scale_factor().map_err(|error| error.to_string())?;
            let current = Bounds {
                x: position.x,
                y: position.y,
                width: i32::try_from(size.width).map_err(|_| "Invalid window width.")?,
                height: i32::try_from(size.height).map_err(|_| "Invalid window height.")?,
            };
            let next = centered_bounds(current, width, height, scale)?;
            if next == current {
                return Ok(());
            }
            window
                .set_size(tauri::PhysicalSize::new(next.width as u32, next.height as u32))
                .map_err(|error| error.to_string())?;
            if let Err(error) = window.set_position(tauri::PhysicalPosition::new(next.x, next.y)) {
                let _ = window.set_size(size);
                return Err(error.to_string());
            }
            Ok(())
        })
        .await
        .map_err(|error| error.to_string())?
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn compact_frame_guard_suppresses_paint_but_preserves_window_messages() {
        use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
        use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
        use windows::Win32::UI::WindowsAndMessaging::{
            CreateWindowExW, DestroyWindow, GWL_EXSTYLE, GWL_STYLE, GetWindowLongPtrW, GetWindowRect,
            SendMessageW, WM_APP, WM_NCACTIVATE, WM_NCPAINT, WS_CAPTION, WS_EX_APPWINDOW, WS_EX_NOACTIVATE,
            WS_EX_WINDOWEDGE, WS_MINIMIZEBOX, WS_POPUP, WS_THICKFRAME,
        };

        #[derive(Default)]
        struct Messages {
            calls: usize,
            last: (u32, usize, isize),
        }
        unsafe extern "system" fn observe(
            hwnd: HWND,
            message: u32,
            wparam: WPARAM,
            lparam: LPARAM,
            _subclass: usize,
            data: usize,
        ) -> LRESULT {
            unsafe {
                if matches!(message, WM_NCPAINT | WM_NCACTIVATE) || message == WM_APP + 1 {
                    let messages = &mut *(data as *mut Messages);
                    messages.calls += 1;
                    messages.last = (message, wparam.0, lparam.0);
                    return LRESULT(37);
                }
                DefSubclassProc(hwnd, message, wparam, lparam)
            }
        }
        struct HiddenWindow(HWND);
        impl Drop for HiddenWindow {
            fn drop(&mut self) {
                let _ = unsafe { DestroyWindow(self.0) };
            }
        }

        // An invisible, thread-owned HWND exercises the real subclass chain
        // without activating a window or changing anything on the desktop.
        let mut messages = Messages::default();
        let window = HiddenWindow(unsafe {
            CreateWindowExW(
                WS_EX_WINDOWEDGE | WS_EX_APPWINDOW,
                windows_core::w!("STATIC"),
                windows_core::w!(""),
                WS_POPUP | WS_CAPTION | WS_THICKFRAME,
                0,
                0,
                280,
                56,
                None,
                None,
                None,
                None,
            )
            .unwrap()
        });
        assert!(
            unsafe { SetWindowSubclass(window.0, Some(observe), 1, &mut messages as *mut Messages as usize) }
                .as_bool()
        );
        let original_style = unsafe { GetWindowLongPtrW(window.0, GWL_STYLE) } as u32;
        let original_ex_style = unsafe { GetWindowLongPtrW(window.0, GWL_EXSTYLE) } as u32;
        let mut original_bounds = RECT::default();
        unsafe { GetWindowRect(window.0, &mut original_bounds).unwrap() };
        apply_shape(window.0, CompactShape::Toolbar, false).unwrap();
        assert_eq!(unsafe { GetWindowLongPtrW(window.0, GWL_STYLE) } as u32 & FRAME_STYLE, 0);
        assert_eq!(unsafe { GetWindowLongPtrW(window.0, GWL_EXSTYLE) } as u32 & FRAME_EX_STYLE, 0);
        let mut compact_bounds = RECT::default();
        unsafe { GetWindowRect(window.0, &mut compact_bounds).unwrap() };
        assert_eq!(compact_bounds, original_bounds, "entering compact must not move or resize");

        // Tao rebuilds native styles for resizability, on-top and taskbar changes.
        // Keep those unrelated updates while preventing its frame bits returning.
        set_native_style(window.0, GWL_STYLE, original_style | WS_MINIMIZEBOX.0).unwrap();
        set_native_style(window.0, GWL_EXSTYLE, original_ex_style | WS_EX_NOACTIVATE.0).unwrap();
        assert_eq!(
            unsafe { GetWindowLongPtrW(window.0, GWL_STYLE) } as u32,
            (original_style | WS_MINIMIZEBOX.0) & !FRAME_STYLE
        );
        assert_eq!(
            unsafe { GetWindowLongPtrW(window.0, GWL_EXSTYLE) } as u32,
            (original_ex_style | WS_EX_NOACTIVATE.0) & !FRAME_EX_STYLE
        );
        messages.calls = 0;

        let send = |message, wparam, lparam| unsafe {
            SendMessageW(window.0, message, Some(WPARAM(wparam)), Some(LPARAM(lparam)))
        };
        assert_eq!(send(WM_NCPAINT, 1, 0), LRESULT(0));
        assert_eq!(messages.calls, 0, "the native frame painter must not run");
        for active in [1, 0] {
            assert_eq!(send(WM_NCACTIVATE, active, 123), LRESULT(37));
            assert_eq!(messages.last, (WM_NCACTIVATE, active, -1));
        }
        assert_eq!(send(WM_APP + 1, 47, 93), LRESULT(37));
        assert_eq!(messages.last, (WM_APP + 1, 47, 93));

        // Full restoration must remove the guard even when the toolbar already
        // has no native clip region. Both original messages then pass unchanged.
        apply_shape(window.0, CompactShape::Full, false).unwrap();
        assert_eq!(
            unsafe { GetWindowLongPtrW(window.0, GWL_STYLE) } as u32,
            original_style | WS_MINIMIZEBOX.0
        );
        assert_eq!(
            unsafe { GetWindowLongPtrW(window.0, GWL_EXSTYLE) } as u32,
            original_ex_style | WS_EX_NOACTIVATE.0
        );
        let mut restored_bounds = RECT::default();
        unsafe { GetWindowRect(window.0, &mut restored_bounds).unwrap() };
        assert_eq!(restored_bounds, original_bounds);
        assert_eq!(send(WM_NCPAINT, 1, 0), LRESULT(37));
        assert_eq!(messages.last, (WM_NCPAINT, 1, 0));
        assert_eq!(send(WM_NCACTIVATE, 0, 123), LRESULT(37));
        assert_eq!(messages.last, (WM_NCACTIVATE, 0, 123));
        // Destroy with a guard installed also exercises WM_NCDESTROY cleanup.
        guard_compact_frame(window.0).unwrap();
    }

    #[test]
    fn rounded_region_uses_client_origin_and_scaled_safety_margin() {
        for (scale, width, height) in [(1.0, 280, 56), (1.25, 350, 70), (1.5, 420, 84), (2.0, 560, 112)] {
            let client = Bounds { x: 8, y: 31, width, height };
            let region = dot_region(client, scale).unwrap();
            assert_eq!(region.width, (68.0 * scale).round() as i32);
            assert_eq!(region.height, (48.0 * scale).round() as i32);
            assert!((region.x * 2 + region.width - (client.x * 2 + width)).abs() <= 1);
            assert!((region.y * 2 + region.height - (client.y * 2 + height)).abs() <= 1);
            assert!(region.x >= client.x && region.y >= client.y);
            assert!(region.x + region.width <= client.x + width);
            assert!(region.y + region.height <= client.y + height);
        }
    }

    #[test]
    fn fractional_dpi_region_stays_within_half_a_pixel_of_client_center() {
        for dpi in [96, 105, 120, 144, 168, 192, 240, 288] {
            let scale = f64::from(dpi) / 96.0;
            let client = Bounds {
                x: 0,
                y: 0,
                width: (280.0 * scale).round() as i32,
                height: (56.0 * scale).round() as i32,
            };
            let region = dot_region(client, scale).unwrap();
            assert!((region.x * 2 + region.width - client.width).abs() <= 1);
            assert!((region.y * 2 + region.height - client.height).abs() <= 1);
            assert_eq!(region, dot_region(client, scale).unwrap());
        }
    }

    #[test]
    fn region_cannot_exceed_surface_or_overflow_coordinates() {
        let client = Bounds { x: 0, y: 0, width: 280, height: 56 };
        for scale in [0.0, -1.0, f64::NAN, f64::INFINITY, 17.0, 0.0001] {
            assert!(dot_region(client, scale).is_err());
        }
        assert!(dot_region(Bounds { height: 47, ..client }, 1.0).is_err());
        assert!(dot_region(Bounds { width: 67, ..client }, 1.0).is_err());
        assert!(dot_region(Bounds { width: 65_537, ..client }, 1.0).is_err());
        assert!(dot_region(Bounds { x: i32::MAX, ..client }, 1.0).is_err());
        assert!(dot_region(Bounds { y: i32::MAX, ..client }, 1.0).is_err());
    }

    #[test]
    fn collapse_keeps_center_at_common_windows_scaling_on_negative_monitors() {
        for (scale, width, height) in [(1.25, 350, 70), (1.5, 420, 84), (2.0, 560, 112)] {
            let current = Bounds { x: -1920, y: -1080, width, height };
            let next = centered_bounds(current, 64.0, 44.0, scale).unwrap();
            assert_eq!(next.width, (64.0 * scale).round() as i32);
            assert_eq!(next.height, (44.0 * scale).round() as i32);
            assert!((current.x * 2 + current.width - next.x * 2 - next.width).abs() <= 1);
            assert!((current.y * 2 + current.height - next.y * 2 - next.height).abs() <= 1);
            assert_eq!(centered_bounds(next, 280.0, 56.0, scale).unwrap(), current);
        }
    }

    #[test]
    fn repeated_round_trips_do_not_accumulate_pixel_drift() {
        for scale in [1.1, 1.25, 1.5, 1.75, 2.0] {
            for (x, y) in [(-1731, -419), (101, 237), (-3, 0)] {
                let original = Bounds {
                    x,
                    y,
                    width: (280.0_f64 * scale).round() as i32,
                    height: (56.0_f64 * scale).round() as i32,
                };
                let mut current = original;
                for _ in 0..100 {
                    current = centered_bounds(current, 64.0, 44.0, scale).unwrap();
                    current = centered_bounds(current, 280.0, 56.0, scale).unwrap();
                    assert_eq!(current, original);
                }
            }
        }
    }

    #[test]
    fn matching_native_size_is_a_no_op() {
        let current = Bounds { x: -120, y: 900, width: 96, height: 66 };
        assert_eq!(centered_bounds(current, 64.0, 44.0, 1.5).unwrap(), current);
    }

    #[test]
    fn malformed_dimensions_scale_and_overflow_are_rejected() {
        let current = Bounds { x: 100, y: 200, width: 280, height: 56 };
        for (width, height) in
            [(280.0, 48.0), (0.0, 0.0), (-48.0, 48.0), (f64::NAN, 48.0), (48.0, f64::INFINITY)]
        {
            assert!(centered_bounds(current, width, height, 1.0).is_err());
        }
        for scale in [0.0, -1.0, f64::NAN, f64::INFINITY, 17.0, 0.0001] {
            assert!(centered_bounds(current, 64.0, 44.0, scale).is_err());
        }
        assert!(centered_bounds(Bounds { width: 0, ..current }, 64.0, 44.0, 1.0).is_err());
        assert!(centered_bounds(Bounds { x: i32::MAX, ..current }, 64.0, 44.0, 1.0).is_err());
        assert!(
            centered_bounds(Bounds { x: i32::MIN, width: 48, height: 48, ..current }, 280.0, 56.0, 1.0)
                .is_err()
        );
    }
}
