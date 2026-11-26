//! Keep a reachable part of each window on a connected display without fitting its whole frame.

#[cfg(any(windows, test))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Rect {
    left: i64,
    top: i64,
    right: i64,
    bottom: i64,
}

#[cfg(any(windows, test))]
impl Rect {
    fn valid(self) -> bool {
        self.right > self.left && self.bottom > self.top
    }
}

/// Find the smallest translation that leaves a useful patch of the drag surface visible.
/// All coordinates are physical, and the surface is relative to the window origin.
#[cfg(any(windows, test))]
fn reachable_origin(
    origin: (i32, i32),
    surface: Rect,
    work_areas: &[Rect],
    scale: f64,
) -> Option<(i32, i32)> {
    if !surface.valid() || !scale.is_finite() || !(0.0..=16.0).contains(&scale) || scale == 0.0 {
        return None;
    }
    let min_width = ((48.0 * scale).ceil() as i64).min(surface.right - surface.left);
    let min_height = ((24.0 * scale).ceil() as i64).min(surface.bottom - surface.top);
    let (x, y) = (i64::from(origin.0), i64::from(origin.1));
    work_areas
        .iter()
        .filter(|area| area.valid())
        .filter_map(|area| {
            let width = min_width.min(area.right - area.left);
            let height = min_height.min(area.bottom - area.top);
            let nx = x.clamp(area.left + width - surface.right, area.right - width - surface.left);
            let ny = y.clamp(area.top + height - surface.bottom, area.bottom - height - surface.top);
            let point = (i32::try_from(nx).ok()?, i32::try_from(ny).ok()?);
            let distance = i128::from(nx - x).pow(2) + i128::from(ny - y).pow(2);
            Some((distance, point))
        })
        .min_by_key(|(distance, _)| *distance)
        .map(|(_, point)| point)
}

#[cfg(windows)]
mod native {
    use super::{Rect, reachable_origin};
    use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
    use windows::Win32::Graphics::Gdi::{
        ClientToScreen, EnumDisplayMonitors, GetMonitorInfoW, GetWindowRgnBox, HDC, HMONITOR, MONITORINFO,
    };
    use windows::Win32::UI::HiDpi::GetDpiForWindow;
    use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
    use windows::Win32::UI::WindowsAndMessaging::{
        GetClientRect, GetWindowRect, IsIconic, IsZoomed, PostMessageW, SWP_HIDEWINDOW, SWP_NOACTIVATE,
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SetWindowPos, WINDOWPOS, WM_APP, WM_DISPLAYCHANGE,
        WM_DPICHANGED, WM_NCDESTROY, WM_SETTINGCHANGE, WM_WINDOWPOSCHANGED, WM_WINDOWPOSCHANGING,
    };
    use windows_core::BOOL;

    const SUBCLASS: usize = 0x4350_5649;
    // Posted after native display/region changes, so Tao finishes its geometry update first.
    const RECHECK: u32 = WM_APP + 0x351;

    fn rect(value: RECT) -> Rect {
        Rect {
            left: value.left.into(),
            top: value.top.into(),
            right: value.right.into(),
            bottom: value.bottom.into(),
        }
    }

    fn monitors() -> Vec<MONITORINFO> {
        unsafe extern "system" fn each(monitor: HMONITOR, _: HDC, _: *mut RECT, data: LPARAM) -> BOOL {
            let mut info =
                MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
            unsafe {
                if GetMonitorInfoW(monitor, &mut info).as_bool() {
                    (*(data.0 as *mut Vec<MONITORINFO>)).push(info);
                }
            }
            BOOL(1)
        }
        let mut result = Vec::<MONITORINFO>::new();
        unsafe {
            let _ = EnumDisplayMonitors(None, None, Some(each), LPARAM(&mut result as *mut _ as isize));
        }
        result
    }

    /// Use the actual clip region in Small mode; the transparent 280px canvas is not reachable.
    fn surface(hwnd: HWND, frame: RECT, width: i32, height: i32, scale: f64) -> Option<Rect> {
        unsafe {
            let mut region = RECT::default();
            if GetWindowRgnBox(hwnd, &mut region).0 > 1 && rect(region).valid() {
                return Some(rect(region));
            }
            let mut client = RECT::default();
            let mut origin = POINT::default();
            if GetClientRect(hwnd, &mut client).is_err() || !ClientToScreen(hwnd, &mut origin).as_bool() {
                return None;
            }
            let left = i64::from(origin.x) - i64::from(frame.left);
            let top = i64::from(origin.y) - i64::from(frame.top);
            let right_inset = i64::from(frame.right) - i64::from(origin.x) - i64::from(client.right);
            Some(Rect {
                left,
                top,
                right: i64::from(width) - right_inset,
                bottom: (top + (32.0 * scale).ceil() as i64).min(i64::from(height)),
            })
        }
    }

    fn corrected(hwnd: HWND, x: i32, y: i32, width: i32, height: i32, current: RECT) -> Option<(i32, i32)> {
        // Windows owns minimized/maximized placements. Never change size, activation or z-order.
        if unsafe { IsIconic(hwnd).as_bool() || IsZoomed(hwnd).as_bool() } || width <= 0 || height <= 0 {
            return None;
        }
        let monitors = monitors();
        let proposed = Rect {
            left: x.into(),
            top: y.into(),
            right: i64::from(x) + i64::from(width),
            bottom: i64::from(y) + i64::from(height),
        };
        // A fullscreen frame must cover the monitor, including its taskbar area.
        if monitors.iter().any(|monitor| rect(monitor.rcMonitor) == proposed) {
            return None;
        }
        let scale = f64::from(unsafe { GetDpiForWindow(hwnd) }) / 96.0;
        let surface = surface(hwnd, current, width, height, scale)?;
        let work_areas: Vec<_> = monitors.iter().map(|monitor| rect(monitor.rcWork)).collect();
        reachable_origin((x, y), surface, &work_areas, scale).filter(|position| *position != (x, y))
    }

    pub(super) fn ensure_reachable(hwnd: HWND) {
        unsafe {
            let mut frame = RECT::default();
            if GetWindowRect(hwnd, &mut frame).is_ok()
                && let Some((x, y)) = corrected(
                    hwnd,
                    frame.left,
                    frame.top,
                    frame.right - frame.left,
                    frame.bottom - frame.top,
                    frame,
                )
            {
                let _ = SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER);
            }
        }
    }

    unsafe extern "system" fn window_proc(
        hwnd: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
        subclass: usize,
        _: usize,
    ) -> LRESULT {
        unsafe {
            if message == WM_NCDESTROY {
                let _ = RemoveWindowSubclass(hwnd, Some(window_proc), subclass);
            }
            if message == RECHECK {
                ensure_reachable(hwnd);
                return LRESULT(0);
            }
            // Preserve default size constraints and native move/resize processing.
            let result = DefSubclassProc(hwnd, message, wparam, lparam);
            if message == WM_WINDOWPOSCHANGING && lparam.0 != 0 {
                let next = &mut *(lparam.0 as *mut WINDOWPOS);
                if !next.flags.contains(SWP_HIDEWINDOW) {
                    let mut frame = RECT::default();
                    if GetWindowRect(hwnd, &mut frame).is_ok() {
                        let (x, y) = if next.flags.contains(SWP_NOMOVE) {
                            (frame.left, frame.top)
                        } else {
                            (next.x, next.y)
                        };
                        let (width, height) = if next.flags.contains(SWP_NOSIZE) {
                            (frame.right - frame.left, frame.bottom - frame.top)
                        } else {
                            (next.cx, next.cy)
                        };
                        if let Some((x, y)) = corrected(hwnd, x, y, width, height, frame) {
                            next.x = x;
                            next.y = y;
                            next.flags &= !SWP_NOMOVE;
                        }
                    }
                }
            } else if matches!(
                message,
                WM_DISPLAYCHANGE | WM_SETTINGCHANGE | WM_DPICHANGED | WM_WINDOWPOSCHANGED
            ) {
                // Also covers restoring a minimized window and clip-region-only changes.
                let _ = PostMessageW(Some(hwnd), RECHECK, WPARAM(0), LPARAM(0));
            }
            result
        }
    }

    pub(super) fn install(hwnd: HWND) -> Result<(), String> {
        if unsafe { SetWindowSubclass(hwnd, Some(window_proc), SUBCLASS, 0) }.as_bool() {
            Ok(())
        } else {
            Err("Could not install window visibility protection.".into())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use windows::Win32::Graphics::Gdi::{CreateRoundRectRgn, SetWindowRgn};
        use windows::Win32::UI::WindowsAndMessaging::{
            CreateWindowExW, DestroyWindow, GWL_STYLE, GetForegroundWindow, GetWindowLongPtrW,
            IsWindowVisible, SWP_NOSENDCHANGING, SendMessageW, SetWindowLongPtrW, WS_EX_NOACTIVATE,
            WS_MAXIMIZE, WS_MINIMIZE, WS_POPUP,
        };

        struct HiddenWindow(HWND);
        impl Drop for HiddenWindow {
            fn drop(&mut self) {
                let _ = unsafe { DestroyWindow(self.0) };
            }
        }

        fn bounds(hwnd: HWND) -> RECT {
            let mut bounds = RECT::default();
            unsafe { GetWindowRect(hwnd, &mut bounds).unwrap() };
            bounds
        }

        #[test]
        fn native_moves_and_clip_changes_keep_a_hidden_test_window_reachable_without_activation() {
            let displays = monitors();
            assert!(!displays.is_empty(), "Native window tests require a display");
            let foreground = unsafe { GetForegroundWindow() };
            // This thread-owned HWND is never shown or activated, and uses no user app state.
            let window = HiddenWindow(unsafe {
                CreateWindowExW(
                    WS_EX_NOACTIVATE,
                    windows_core::w!("STATIC"),
                    windows_core::w!(""),
                    WS_POPUP,
                    displays[0].rcWork.left + 100,
                    displays[0].rcWork.top + 100,
                    280,
                    56,
                    None,
                    None,
                    None,
                    None,
                )
                .unwrap()
            });
            install(window.0).unwrap();
            let off_x = displays.iter().map(|m| m.rcWork.right).max().unwrap() + 500;
            let off_y = displays.iter().map(|m| m.rcWork.bottom).max().unwrap() + 500;
            unsafe {
                SetWindowPos(window.0, None, off_x, off_y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)
                    .unwrap();
            }
            let moved = bounds(window.0);
            assert_ne!((moved.left, moved.top), (off_x, off_y));
            assert_eq!((moved.right - moved.left, moved.bottom - moved.top), (280, 56));
            assert_eq!(corrected(window.0, moved.left, moved.top, 280, 56, moved), None);
            // A normal partial placement is retained exactly on a second move.
            unsafe {
                SetWindowPos(
                    window.0,
                    None,
                    moved.left,
                    moved.top,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
                )
                .unwrap();
            }
            assert_eq!(bounds(window.0), moved);

            // The Small launcher's region, rather than its empty canvas, must remain reachable.
            unsafe {
                let region = CreateRoundRectRgn(106, 4, 174, 52, 20, 20);
                assert_ne!(SetWindowRgn(window.0, Some(region), false), 0);
                SendMessageW(window.0, RECHECK, Some(WPARAM(0)), Some(LPARAM(0)));
            }
            let small = bounds(window.0);
            assert_eq!((small.right - small.left, small.bottom - small.top), (280, 56));
            assert_eq!(corrected(window.0, small.left, small.top, 280, 56, small), None);
            let scale = f64::from(unsafe { GetDpiForWindow(window.0) }) / 96.0;
            assert!(displays.iter().any(|monitor| {
                let work = monitor.rcWork;
                let width = (small.left + 174).min(work.right) - (small.left + 106).max(work.left);
                let height = (small.top + 52).min(work.bottom) - (small.top + 4).max(work.top);
                width >= ((48.0 * scale).ceil() as i32).min(68)
                    && height >= ((24.0 * scale).ceil() as i32).min(48)
            }));

            // Simulate a stale position after display removal, bypassing the proposed-move guard.
            unsafe {
                SetWindowPos(
                    window.0,
                    None,
                    off_x,
                    off_y,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSENDCHANGING,
                )
                .unwrap();
                SendMessageW(window.0, WM_DISPLAYCHANGE, Some(WPARAM(0)), Some(LPARAM(0)));
                SendMessageW(window.0, RECHECK, Some(WPARAM(0)), Some(LPARAM(0)));
            }
            let recovered = bounds(window.0);
            assert_ne!((recovered.left, recovered.top), (off_x, off_y));
            assert_eq!(corrected(window.0, recovered.left, recovered.top, 280, 56, recovered), None);
            // Minimize/maximize state is owned by Windows, even for off-display bounds.
            unsafe {
                let style = GetWindowLongPtrW(window.0, GWL_STYLE);
                for flag in [WS_MINIMIZE, WS_MAXIMIZE] {
                    SetWindowLongPtrW(window.0, GWL_STYLE, style | flag.0 as isize);
                    assert_eq!(corrected(window.0, -32000, -32000, 280, 56, recovered), None);
                }
                SetWindowLongPtrW(window.0, GWL_STYLE, style);
            }
            let fullscreen = displays[0].rcMonitor;
            assert_eq!(
                corrected(
                    window.0,
                    fullscreen.left,
                    fullscreen.top,
                    fullscreen.right - fullscreen.left,
                    fullscreen.bottom - fullscreen.top,
                    recovered
                ),
                None
            );
            assert!(!unsafe { IsWindowVisible(window.0) }.as_bool());
            assert_eq!(unsafe { GetForegroundWindow() }, foreground);
        }
    }
}

pub(crate) fn setup(app: &tauri::AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    {
        use tauri::Manager;
        for label in ["main", "overlay"] {
            if let Some(window) = app.get_webview_window(label) {
                native::install(window.hwnd().map_err(|error| error.to_string())?)?;
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

    fn area(x: i64, y: i64, width: i64, height: i64) -> Rect {
        Rect { left: x, top: y, right: x + width, bottom: y + height }
    }

    #[test]
    fn leaves_partly_offscreen_frames_alone() {
        let display = [area(0, 0, 1920, 1040)];
        let header = area(0, 0, 800, 32);
        for point in [(-700, 0), (1800, 400), (100, 1000), (100, -8), (100, 200)] {
            assert_eq!(reachable_origin(point, header, &display, 1.0), Some(point));
        }
    }

    #[test]
    fn retains_a_reachable_header_at_each_edge_and_corner() {
        let display = [area(0, 0, 1920, 1040)];
        let header = area(0, 0, 800, 32);
        for (point, expected) in [
            ((-900, 200), (-752, 200)),
            ((2200, 200), (1872, 200)),
            ((200, -800), (200, -8)),
            ((200, 1100), (200, 1016)),
            ((2200, 1100), (1872, 1016)),
        ] {
            assert_eq!(reachable_origin(point, header, &display, 1.0), Some(expected));
        }
    }

    #[test]
    fn considers_each_real_monitor_including_negative_origins_and_gaps() {
        let displays = [area(-1920, -200, 1920, 1080), area(320, 0, 1920, 1040)];
        let header = area(0, 0, 280, 32);
        assert_eq!(reachable_origin((-1000, -100), header, &displays, 1.0), Some((-1000, -100)));
        assert_eq!(reachable_origin((0, 100), header, &displays, 1.0), Some((-48, 100)));
        assert_eq!(reachable_origin((2500, 100), header, &displays, 1.0), Some((2192, 100)));
    }

    #[test]
    fn recovers_from_a_disconnected_display_with_the_smallest_move() {
        let header = area(0, 0, 800, 32);
        assert_eq!(reachable_origin((2400, 200), header, &[area(0, 0, 1920, 1040)], 1.0), Some((1872, 200)));
    }

    #[test]
    fn protects_the_dot_itself_not_its_invisible_canvas_at_every_scale() {
        for scale in [1.0, 1.25, 1.5, 2.0] {
            let px = |n: f64| (n * scale).round() as i64;
            let dot = area(px(106.0), px(4.0), px(68.0), px(48.0));
            let display = [area(0, 0, 1920, 1040)];
            let actual = reachable_origin((-1000, 100), dot, &display, scale).unwrap();
            assert_eq!(i64::from(actual.0) + dot.right, (48.0 * scale).ceil() as i64);
            assert_eq!(actual.1, 100);
        }
    }

    #[test]
    fn handles_tiny_work_areas_missing_displays_and_extreme_coordinates() {
        let header = area(0, 0, 800, 32);
        let tiny = [area(0, 0, 20, 10)];
        assert_eq!(reachable_origin((900, 100), header, &tiny, 1.0), Some((0, 0)));
        assert_eq!(reachable_origin((0, 0), header, &[], 1.0), None);
        for scale in [0.0, f64::NAN, f64::INFINITY] {
            assert_eq!(reachable_origin((0, 0), header, &tiny, scale), None);
        }
        assert_eq!(
            reachable_origin((i32::MAX, i32::MIN), header, &[area(0, 0, 1920, 1040)], 1.0),
            Some((1872, -8))
        );
    }
}
