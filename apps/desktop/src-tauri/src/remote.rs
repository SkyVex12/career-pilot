//! Guest seats' remote input (ARCHITECTURE.md, "Guest seats", phase 5): a guest the host lets
//! control their shared screen moves the mouse and types on the host's computer.
//!
//! The decisions are made in the window (src/screen.ts): which guest controls (only a seat
//! holding `device.control`, and only after the host pressed Let them control), and on which
//! monitor (the one being shared). Here the app carries the events out, and only between
//! `begin` and `end`: a guest's coordinates (0 to 1 across the shared picture) land on that
//! monitor alone, keys go by their physical position (scan codes, so the host's own keyboard
//! layout applies), and every key and button a guest holds is let go when control ends, so
//! nothing stays pressed. Windows itself keeps injected input out of elevated windows and the
//! secure desktop (Ctrl+Alt+Del, UAC prompts). The Stop control hotkey ends it from anywhere.

use serde::{Deserialize, Serialize};
use std::sync::Mutex;

/// One event from the guest, as the window passes it on.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "t", rename_all = "snake_case")]
pub enum Input {
    Move { x: f64, y: f64 },
    Down { button: u8, x: f64, y: f64 },
    Up { button: u8, x: f64, y: f64 },
    Wheel { dx: f64, dy: f64 },
    KeyDown { code: String },
    KeyUp { code: String },
}

/// A rectangle in physical pixels on the virtual desktop.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

/// A monitor, for matching the shared picture to it.
#[derive(Debug, Clone, Serialize)]
pub struct Monitor {
    pub id: String,
    pub rect: Rect,
    pub primary: bool,
    /// Backing pixels for matching Retina screen video; input uses rect desktop points.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pixels: Option<(u32, u32)>,
    /// WebRTC's Windows source ID is the EnumDisplayDevices index, not monitor enumeration order.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub capture_id: Option<u32>,
}

/// What to send Windows, already checked and placed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    /// The pointer to this physical pixel.
    MoveTo {
        x: i32,
        y: i32,
    },
    /// 0 left, 1 middle, 2 right (the browser's numbering).
    Button {
        button: u8,
        down: bool,
    },
    /// In Windows' wheel units (120 a notch): positive is up, and right.
    Wheel {
        vertical: i32,
        horizontal: i32,
    },
    Key {
        scan: u16,
        extended: bool,
        down: bool,
    },
}

/// At most this many events in one call: the window sends small batches.
pub const MAX_BATCH: usize = 200;

/// Control in progress: the monitor events land on, and what the guest holds down now.
#[derive(Debug, Default)]
pub struct Remote {
    target: Option<Rect>,
    keys: Vec<(u16, bool)>,
    buttons: Vec<u8>,
}

impl Remote {
    pub const fn new() -> Self {
        Self { target: None, keys: Vec::new(), buttons: Vec::new() }
    }

    pub fn active(&self) -> bool {
        self.target.is_some()
    }

    /// Control begins (or moves to another monitor): what was held is let go first.
    pub fn begin(&mut self, target: Rect) -> Vec<Op> {
        let release = self.release();
        self.target = (target.w > 0 && target.h > 0).then_some(target);
        release
    }

    /// Control ends: everything the guest held is let go, and nothing more is accepted.
    pub fn end(&mut self) -> Vec<Op> {
        self.target = None;
        self.release()
    }

    fn release(&mut self) -> Vec<Op> {
        let mut ops: Vec<Op> =
            self.keys.drain(..).map(|(scan, extended)| Op::Key { scan, extended, down: false }).collect();
        ops.extend(self.buttons.drain(..).map(|button| Op::Button { button, down: false }));
        ops
    }

    fn point(&self, x: f64, y: f64) -> Option<Op> {
        let r = self.target?;
        if !(x.is_finite() && y.is_finite()) {
            return None;
        }
        let px = r.x + (x.clamp(0.0, 1.0) * f64::from(r.w - 1)).round() as i32;
        let py = r.y + (y.clamp(0.0, 1.0) * f64::from(r.h - 1)).round() as i32;
        Some(Op::MoveTo { x: px, y: py })
    }

    /// The guest's events as what to send, keeping track of what is held. Refused when control
    /// is not on; unknown keys and buttons are skipped.
    pub fn plan(&mut self, events: &[Input]) -> Result<Vec<Op>, &'static str> {
        if !self.active() {
            return Err("no one controls the screen now");
        }
        if events.len() > MAX_BATCH {
            return Err("too many events at once");
        }
        let mut ops = Vec::new();
        for e in events {
            match e {
                Input::Move { x, y } => ops.extend(self.point(*x, *y)),
                Input::Down { button, x, y } | Input::Up { button, x, y } => {
                    if *button > 2 {
                        continue;
                    }
                    let down = matches!(e, Input::Down { .. });
                    let Some(point) = self.point(*x, *y) else { continue };
                    ops.push(point);
                    let held = self.buttons.contains(button);
                    if down && !held {
                        self.buttons.push(*button);
                    } else if !down && held {
                        self.buttons.retain(|b| b != button);
                    } else if !down {
                        continue; // never let go of what the guest did not press
                    }
                    ops.push(Op::Button { button: *button, down });
                }
                Input::Wheel { dx, dy } => {
                    let unit = |d: f64| {
                        if d.is_finite() { (d * 1.2).round().clamp(-1200.0, 1200.0) as i32 } else { 0 }
                    };
                    // The browser's positive deltaY scrolls down; Windows' positive wheel is up.
                    let (vertical, horizontal) = (-unit(*dy), unit(*dx));
                    if vertical != 0 || horizontal != 0 {
                        ops.push(Op::Wheel { vertical, horizontal });
                    }
                }
                Input::KeyDown { code } | Input::KeyUp { code } => {
                    let Some((scan, extended)) = scan_code(code) else { continue };
                    let down = matches!(e, Input::KeyDown { .. });
                    let held = self.keys.contains(&(scan, extended));
                    if down && !held {
                        self.keys.push((scan, extended));
                    } else if !down && held {
                        self.keys.retain(|k| *k != (scan, extended));
                    } else if !down {
                        continue;
                    }
                    ops.push(Op::Key { scan, extended, down });
                }
            }
        }
        Ok(ops)
    }
}

/// A key by its physical position (the browser's `KeyboardEvent.code`) as a set 1 scan code,
/// and whether it is an extended key.
pub fn scan_code(code: &str) -> Option<(u16, bool)> {
    const LETTERS: [u16; 26] = [
        0x1E, 0x30, 0x2E, 0x20, 0x12, 0x21, 0x22, 0x23, 0x17, 0x24, 0x25, 0x26, 0x32, 0x31, 0x18, 0x19, 0x10,
        0x13, 0x1F, 0x14, 0x16, 0x2F, 0x11, 0x2D, 0x15, 0x2C,
    ];
    if let Some(l) = code.strip_prefix("Key") {
        let c = l.as_bytes();
        if c.len() == 1 && c[0].is_ascii_uppercase() {
            return Some((LETTERS[usize::from(c[0] - b'A')], false));
        }
    }
    if let Some(d) = code.strip_prefix("Digit")
        && let Ok(n @ 0..=9) = d.parse::<u16>()
    {
        return Some((if n == 0 { 0x0B } else { 0x01 + n }, false));
    }
    if let Some(f) = code.strip_prefix('F')
        && let Ok(n @ 1..=12) = f.parse::<u16>()
    {
        return Some((if n <= 10 { 0x3A + n } else { 0x57 + n - 11 }, false));
    }
    let plain = |s: u16| Some((s, false));
    let ext = |s: u16| Some((s, true));
    match code {
        "Escape" => plain(0x01),
        "Minus" => plain(0x0C),
        "Equal" => plain(0x0D),
        "Backspace" => plain(0x0E),
        "Tab" => plain(0x0F),
        "BracketLeft" => plain(0x1A),
        "BracketRight" => plain(0x1B),
        "Enter" => plain(0x1C),
        "ControlLeft" => plain(0x1D),
        "Semicolon" => plain(0x27),
        "Quote" => plain(0x28),
        "Backquote" => plain(0x29),
        "ShiftLeft" => plain(0x2A),
        "Backslash" => plain(0x2B),
        "Comma" => plain(0x33),
        "Period" => plain(0x34),
        "Slash" => plain(0x35),
        "ShiftRight" => plain(0x36),
        "NumpadMultiply" => plain(0x37),
        "AltLeft" => plain(0x38),
        "Space" => plain(0x39),
        "CapsLock" => plain(0x3A),
        "NumLock" => plain(0x45),
        "ScrollLock" => plain(0x46),
        "Numpad7" => plain(0x47),
        "Numpad8" => plain(0x48),
        "Numpad9" => plain(0x49),
        "NumpadSubtract" => plain(0x4A),
        "Numpad4" => plain(0x4B),
        "Numpad5" => plain(0x4C),
        "Numpad6" => plain(0x4D),
        "NumpadAdd" => plain(0x4E),
        "Numpad1" => plain(0x4F),
        "Numpad2" => plain(0x50),
        "Numpad3" => plain(0x51),
        "Numpad0" => plain(0x52),
        "NumpadDecimal" => plain(0x53),
        "IntlBackslash" => plain(0x56),
        "NumpadEnter" => ext(0x1C),
        "ControlRight" => ext(0x1D),
        "NumpadDivide" => ext(0x35),
        "PrintScreen" => ext(0x37),
        "AltRight" => ext(0x38),
        "Home" => ext(0x47),
        "ArrowUp" => ext(0x48),
        "PageUp" => ext(0x49),
        "ArrowLeft" => ext(0x4B),
        "ArrowRight" => ext(0x4D),
        "End" => ext(0x4F),
        "ArrowDown" => ext(0x50),
        "PageDown" => ext(0x51),
        "Insert" => ext(0x52),
        "Delete" => ext(0x53),
        "MetaLeft" => ext(0x5B),
        "MetaRight" => ext(0x5C),
        "ContextMenu" => ext(0x5D),
        _ => None,
    }
}

/// The one control state of the app.
pub static REMOTE: Mutex<Remote> = Mutex::new(Remote::new());

/// Begin control on `target`, letting go of anything held before.
pub fn begin(target: Rect) -> Result<(), String> {
    if !cfg!(any(windows, target_os = "macos")) {
        return Err("Remote input is supported on Windows and macOS.".into());
    }
    #[cfg(target_os = "macos")]
    {
        crate::remote_macos::permission(true)?;
    }
    if !valid_target(target) {
        return Err("The shared display changed. Share it again before allowing control.".into());
    }
    let mut remote = REMOTE.lock().unwrap();
    let ops = remote.begin(target);
    send(&ops);
    Ok(())
}

/// End control now (the window, the hotkey, the call ending): let go of everything.
pub fn end() {
    let mut remote = REMOTE.lock().unwrap();
    let ops = remote.end();
    send(&ops);
}

/// Carry out a batch of the controlling guest's events.
pub fn input(events: &[Input]) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if let Err(error) = crate::remote_macos::permission(false) {
        end();
        return Err(error);
    }
    let mut remote = REMOTE.lock().unwrap();
    if remote.target.is_some_and(|target| !valid_target(target)) {
        send(&remote.end());
        return Err("The shared display changed. Stop and restart screen sharing.".into());
    }
    let ops = remote.plan(events).map_err(str::to_string)?;
    send(&ops);
    Ok(())
}

/// Validate bounds before every input batch without re-enumerating display adapters.
#[cfg(windows)]
fn valid_target(target: Rect) -> bool {
    use windows::Win32::Foundation::RECT;
    use windows::Win32::Graphics::Gdi::{
        GetMonitorInfoW, MONITOR_DEFAULTTONULL, MONITORINFO, MonitorFromRect,
    };
    let Some(right) = target.x.checked_add(target.w) else { return false };
    let Some(bottom) = target.y.checked_add(target.h) else { return false };
    if target.w <= 0 || target.h <= 0 {
        return false;
    }
    let rect = RECT { left: target.x, top: target.y, right, bottom };
    let monitor = unsafe { MonitorFromRect(&rect, MONITOR_DEFAULTTONULL) };
    let mut info = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
    unsafe { GetMonitorInfoW(monitor, &mut info) }.as_bool()
        && info.rcMonitor.left == rect.left
        && info.rcMonitor.top == rect.top
        && info.rcMonitor.right == rect.right
        && info.rcMonitor.bottom == rect.bottom
}

#[cfg(not(windows))]
fn valid_target(target: Rect) -> bool {
    monitors().iter().any(|m| m.rect == target)
}

#[cfg(windows)]
fn send(ops: &[Op]) {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBD_EVENT_FLAGS, KEYBDINPUT, KEYEVENTF_EXTENDEDKEY,
        KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, MOUSE_EVENT_FLAGS, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_HWHEEL,
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP,
        MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_VIRTUALDESK,
        MOUSEEVENTF_WHEEL, MOUSEINPUT, SendInput, VIRTUAL_KEY,
    };
    if ops.is_empty() {
        return;
    }
    let desk = virtual_desktop();
    let mouse = |dx: i32, dy: i32, data: i32, flags: MOUSE_EVENT_FLAGS| INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT { dx, dy, mouseData: data as u32, dwFlags: flags, time: 0, dwExtraInfo: 0 },
        },
    };
    let mut inputs = Vec::with_capacity(ops.len() * 2);
    for op in ops {
        match *op {
            Op::MoveTo { x, y } => {
                // Absolute input is 0..65535 across the whole virtual desktop.
                let nx = ((i64::from(x - desk.x) * 65535) / i64::from((desk.w - 1).max(1))) as i32;
                let ny = ((i64::from(y - desk.y) * 65535) / i64::from((desk.h - 1).max(1))) as i32;
                inputs.push(mouse(
                    nx,
                    ny,
                    0,
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                ));
            }
            Op::Button { button, down } => {
                let flags = match (button, down) {
                    (0, true) => MOUSEEVENTF_LEFTDOWN,
                    (0, false) => MOUSEEVENTF_LEFTUP,
                    (1, true) => MOUSEEVENTF_MIDDLEDOWN,
                    (1, false) => MOUSEEVENTF_MIDDLEUP,
                    (2, true) => MOUSEEVENTF_RIGHTDOWN,
                    _ => MOUSEEVENTF_RIGHTUP,
                };
                inputs.push(mouse(0, 0, 0, flags));
            }
            Op::Wheel { vertical, horizontal } => {
                if vertical != 0 {
                    inputs.push(mouse(0, 0, vertical, MOUSEEVENTF_WHEEL));
                }
                if horizontal != 0 {
                    inputs.push(mouse(0, 0, horizontal, MOUSEEVENTF_HWHEEL));
                }
            }
            Op::Key { scan, extended, down } => {
                let mut flags = KEYEVENTF_SCANCODE;
                if extended {
                    flags |= KEYEVENTF_EXTENDEDKEY;
                }
                if !down {
                    flags |= KEYEVENTF_KEYUP;
                }
                inputs.push(INPUT {
                    r#type: INPUT_KEYBOARD,
                    Anonymous: INPUT_0 {
                        ki: KEYBDINPUT {
                            wVk: VIRTUAL_KEY(0),
                            wScan: scan,
                            dwFlags: KEYBD_EVENT_FLAGS(flags.0),
                            time: 0,
                            dwExtraInfo: 0,
                        },
                    },
                });
            }
        }
    }
    let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
    if sent as usize != inputs.len() {
        tracing::warn!("remote input: {sent} of {} events went through", inputs.len());
    }
}

#[cfg(target_os = "macos")]
fn send(ops: &[Op]) {
    crate::remote_macos::send(ops);
}

#[cfg(not(any(windows, target_os = "macos")))]
fn send(_ops: &[Op]) {}

#[cfg(windows)]
fn virtual_desktop() -> Rect {
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    };
    unsafe {
        Rect {
            x: GetSystemMetrics(SM_XVIRTUALSCREEN),
            y: GetSystemMetrics(SM_YVIRTUALSCREEN),
            w: GetSystemMetrics(SM_CXVIRTUALSCREEN),
            h: GetSystemMetrics(SM_CYVIRTUALSCREEN),
        }
    }
}

/// The monitors, in physical pixels (the app is per-monitor DPI aware).
#[cfg(windows)]
pub fn monitors() -> Vec<Monitor> {
    use windows::Win32::Foundation::{LPARAM, RECT};
    use windows::Win32::Graphics::Gdi::{
        DISPLAY_DEVICE_ACTIVE, DISPLAY_DEVICEW, EnumDisplayDevicesW, EnumDisplayMonitors, GetMonitorInfoW,
        HDC, HMONITOR, MONITORINFO, MONITORINFOEXW,
    };
    use windows::core::BOOL;

    unsafe extern "system" fn each(m: HMONITOR, _: HDC, _: *mut RECT, data: LPARAM) -> BOOL {
        let out = unsafe { &mut *(data.0 as *mut Vec<Monitor>) };
        let mut info = MONITORINFOEXW::default();
        info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
        if unsafe { GetMonitorInfoW(m, &mut info.monitorInfo as *mut MONITORINFO) }.as_bool() {
            let r = info.monitorInfo.rcMonitor;
            let end = info.szDevice.iter().position(|&c| c == 0).unwrap_or(info.szDevice.len());
            out.push(Monitor {
                id: String::from_utf16_lossy(&info.szDevice[..end]),
                rect: Rect { x: r.left, y: r.top, w: r.right - r.left, h: r.bottom - r.top },
                primary: info.monitorInfo.dwFlags & 1 != 0, // MONITORINFOF_PRIMARY
                pixels: None,
                capture_id: None,
            });
        }
        BOOL(1)
    }

    let mut out: Vec<Monitor> = Vec::new();
    unsafe {
        let _ = EnumDisplayMonitors(None, None, Some(each), LPARAM(&mut out as *mut Vec<Monitor> as isize));
    }
    // Chromium's screen:<id>:0 track label uses this exact device index. Matching device
    // names preserves the shared display across different monitor orderings and DPI values.
    let mut index = 0;
    loop {
        let mut device =
            DISPLAY_DEVICEW { cb: std::mem::size_of::<DISPLAY_DEVICEW>() as u32, ..Default::default() };
        if !unsafe { EnumDisplayDevicesW(None, index, &mut device, 0) }.as_bool() {
            break;
        }
        if device.StateFlags & DISPLAY_DEVICE_ACTIVE != Default::default() {
            let end = device.DeviceName.iter().position(|&c| c == 0).unwrap_or(device.DeviceName.len());
            let name = String::from_utf16_lossy(&device.DeviceName[..end]);
            if let Some(monitor) = out.iter_mut().find(|m| m.id == name) {
                monitor.capture_id = Some(index);
            }
        }
        index += 1;
    }
    out
}

#[cfg(target_os = "macos")]
pub fn monitors() -> Vec<Monitor> {
    crate::remote_macos::monitors()
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn monitors() -> Vec<Monitor> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: Rect = Rect { x: -1920, y: 0, w: 1920, h: 1080 };

    #[cfg(windows)]
    #[test]
    fn capture_source_ids_match_active_display_devices() {
        use windows::Win32::Graphics::Gdi::{DISPLAY_DEVICEW, EnumDisplayDevicesW};
        for monitor in monitors() {
            let id = monitor.capture_id.expect("every active monitor has a capture device index");
            let mut device =
                DISPLAY_DEVICEW { cb: std::mem::size_of::<DISPLAY_DEVICEW>() as u32, ..Default::default() };
            assert!(unsafe { EnumDisplayDevicesW(None, id, &mut device, 0) }.as_bool());
            let end = device.DeviceName.iter().position(|&c| c == 0).unwrap_or(device.DeviceName.len());
            assert_eq!(monitor.id, String::from_utf16_lossy(&device.DeviceName[..end]));
        }
    }

    #[cfg(windows)]
    #[test]
    fn control_accepts_only_current_whole_display_bounds() {
        assert!(!valid_target(Rect { x: 0, y: 0, w: 0, h: 0 }));
        assert!(!valid_target(Rect { x: i32::MAX, y: 0, w: 1, h: 1 }));
        for monitor in monitors() {
            assert!(valid_target(monitor.rect));
            assert!(!valid_target(Rect { w: monitor.rect.w - 1, ..monitor.rect }));
        }
    }

    #[test]
    fn invalid_click_coordinates_never_press_at_the_last_pointer_position() {
        let mut remote = Remote::new();
        remote.begin(SCREEN);
        assert!(remote.plan(&[Input::Down { button: 0, x: f64::NAN, y: 0.5 }]).unwrap().is_empty());
        assert!(remote.end().is_empty());
    }

    fn on() -> Remote {
        let mut r = Remote::new();
        r.begin(SCREEN);
        r
    }

    #[test]
    fn nothing_happens_without_control() {
        assert!(Remote::new().plan(&[Input::Move { x: 0.5, y: 0.5 }]).is_err());
        let mut r = on();
        r.end();
        assert!(r.plan(&[Input::KeyDown { code: "KeyA".into() }]).is_err());
    }

    #[test]
    fn the_guest_lands_on_the_shared_monitor_only() {
        let mut r = on();
        let ops = r.plan(&[
            Input::Move { x: 0.0, y: 0.0 },
            Input::Move { x: 1.0, y: 1.0 },
            Input::Move { x: 7.0, y: -3.0 },
        ]);
        assert_eq!(
            ops.unwrap(),
            vec![Op::MoveTo { x: -1920, y: 0 }, Op::MoveTo { x: -1, y: 1079 }, Op::MoveTo { x: -1, y: 0 }]
        );
        assert_eq!(r.plan(&[Input::Move { x: f64::NAN, y: 0.5 }]).unwrap(), vec![]);
    }

    #[test]
    fn a_click_moves_then_presses_and_never_lets_go_of_what_it_did_not_press() {
        let mut r = on();
        let ops = r
            .plan(&[Input::Down { button: 0, x: 0.5, y: 0.5 }, Input::Up { button: 0, x: 0.5, y: 0.5 }])
            .unwrap();
        assert_eq!(
            ops,
            vec![
                Op::MoveTo { x: -960, y: 540 },
                Op::Button { button: 0, down: true },
                Op::MoveTo { x: -960, y: 540 },
                Op::Button { button: 0, down: false },
            ]
        );
        assert_eq!(
            r.plan(&[Input::Up { button: 2, x: 0.0, y: 0.0 }]).unwrap(),
            vec![Op::MoveTo { x: -1920, y: 0 }]
        );
        assert_eq!(r.plan(&[Input::Down { button: 7, x: 0.0, y: 0.0 }]).unwrap(), vec![]);
    }

    #[test]
    fn ending_control_lets_go_of_every_key_and_button_held() {
        let mut r = on();
        r.plan(&[
            Input::KeyDown { code: "ControlLeft".into() },
            Input::KeyDown { code: "ArrowUp".into() },
            Input::KeyDown { code: "ArrowUp".into() }, // auto-repeat: still one key held
            Input::Down { button: 2, x: 0.1, y: 0.1 },
        ])
        .unwrap();
        let released = r.end();
        assert_eq!(
            released,
            vec![
                Op::Key { scan: 0x1D, extended: false, down: false },
                Op::Key { scan: 0x48, extended: true, down: false },
                Op::Button { button: 2, down: false },
            ]
        );
        assert!(r.end().is_empty());
    }

    #[test]
    fn the_wheel_follows_windows_direction_and_is_bounded() {
        let mut r = on();
        assert_eq!(
            r.plan(&[Input::Wheel { dx: 0.0, dy: 100.0 }, Input::Wheel { dx: 50.0, dy: -1e9 }]).unwrap(),
            vec![Op::Wheel { vertical: -120, horizontal: 0 }, Op::Wheel { vertical: 1200, horizontal: 60 }]
        );
    }

    #[test]
    fn keys_go_by_position() {
        assert_eq!(scan_code("KeyA"), Some((0x1E, false)));
        assert_eq!(scan_code("KeyZ"), Some((0x2C, false)));
        assert_eq!(scan_code("Digit1"), Some((0x02, false)));
        assert_eq!(scan_code("Digit0"), Some((0x0B, false)));
        assert_eq!(scan_code("F1"), Some((0x3B, false)));
        assert_eq!(scan_code("F10"), Some((0x44, false)));
        assert_eq!(scan_code("F12"), Some((0x58, false)));
        assert_eq!(scan_code("ArrowLeft"), Some((0x4B, true)));
        assert_eq!(scan_code("MetaLeft"), Some((0x5B, true)));
        assert_eq!(scan_code("Keya"), None);
        assert_eq!(scan_code("F13"), None);
        assert_eq!(scan_code("MediaPlayPause"), None);
    }

    #[test]
    fn too_many_events_at_once_are_refused() {
        let mut r = on();
        let many = vec![Input::Move { x: 0.5, y: 0.5 }; MAX_BATCH + 1];
        assert!(r.plan(&many).is_err());
    }

    #[test]
    fn events_arrive_as_the_window_sends_them() {
        let e: Vec<Input> = serde_json::from_str(
            r#"[{"t":"move","x":0.5,"y":0.25},{"t":"key_down","code":"KeyA"},{"t":"wheel","dx":0,"dy":3}]"#,
        )
        .unwrap();
        assert_eq!(e[1], Input::KeyDown { code: "KeyA".into() });
    }
}
