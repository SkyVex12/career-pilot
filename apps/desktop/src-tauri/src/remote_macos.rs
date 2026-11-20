//! Quartz input backend. The shared Remote planner owns authorization, bounds, and releases.
//! Coordinates are CoreGraphics desktop points (not Retina backing pixels).
#[cfg(target_os = "macos")]
use super::remote::Op;

/// Convert the planner's physical PC scan code to the Mac hardware key position.
/// This table is independent of the active keyboard layout, as on Windows.
pub fn keycode(scan: u16, extended: bool) -> Option<u16> {
    let key = if extended {
        match scan {
            0x1c => 76,
            0x1d => 62,
            0x35 => 75,
            0x38 => 61,
            0x47 => 115,
            0x48 => 126,
            0x49 => 116,
            0x4b => 123,
            0x4d => 124,
            0x4f => 119,
            0x50 => 125,
            0x51 => 121,
            0x52 => 114,
            0x53 => 117,
            0x5b => 55,
            0x5c => 54,
            _ => return None,
        }
    } else {
        match scan {
            0x01 => 53,
            0x02 => 18,
            0x03 => 19,
            0x04 => 20,
            0x05 => 21,
            0x06 => 23,
            0x07 => 22,
            0x08 => 26,
            0x09 => 28,
            0x0a => 25,
            0x0b => 29,
            0x0c => 27,
            0x0d => 24,
            0x0e => 51,
            0x0f => 48,
            0x10 => 12,
            0x11 => 13,
            0x12 => 14,
            0x13 => 15,
            0x14 => 17,
            0x15 => 16,
            0x16 => 32,
            0x17 => 34,
            0x18 => 31,
            0x19 => 35,
            0x1a => 33,
            0x1b => 30,
            0x1c => 36,
            0x1d => 59,
            0x1e => 0,
            0x1f => 1,
            0x20 => 2,
            0x21 => 3,
            0x22 => 5,
            0x23 => 4,
            0x24 => 38,
            0x25 => 40,
            0x26 => 37,
            0x27 => 41,
            0x28 => 39,
            0x29 => 50,
            0x2a => 56,
            0x2b => 42,
            0x2c => 6,
            0x2d => 7,
            0x2e => 8,
            0x2f => 9,
            0x30 => 11,
            0x31 => 45,
            0x32 => 46,
            0x33 => 43,
            0x34 => 47,
            0x35 => 44,
            0x36 => 60,
            0x37 => 67,
            0x38 => 58,
            0x39 => 49,
            0x3a => 57,
            0x3b => 122,
            0x3c => 120,
            0x3d => 99,
            0x3e => 118,
            0x3f => 96,
            0x40 => 97,
            0x41 => 98,
            0x42 => 100,
            0x43 => 101,
            0x44 => 109,
            0x45 => 71,
            0x47 => 89,
            0x48 => 91,
            0x49 => 92,
            0x4a => 78,
            0x4b => 86,
            0x4c => 87,
            0x4d => 88,
            0x4e => 69,
            0x4f => 83,
            0x50 => 84,
            0x51 => 85,
            0x52 => 82,
            0x53 => 65,
            0x56 => 10,
            0x57 => 103,
            0x58 => 111,
            _ => return None,
        }
    };
    Some(key)
}

#[derive(Default)]
pub struct Modifiers(Vec<u16>);
impl Modifiers {
    pub fn update(&mut self, key: u16, down: bool) {
        if down && !self.0.contains(&key) {
            self.0.push(key);
        }
        if !down {
            self.0.retain(|k| *k != key);
        }
    }
    pub fn flags(&self) -> u64 {
        self.0.iter().fold(0, |flags, key| flags | modifier(*key))
    }
}
fn modifier(key: u16) -> u64 {
    match key {
        56 | 60 => 1 << 17,
        59 | 62 => 1 << 18,
        58 | 61 => 1 << 19,
        54 | 55 => 1 << 20,
        _ => 0,
    }
}

/// Mouse movement must become a drag while a button is held.
fn motion(buttons: &[u8]) -> (u32, u32) {
    if buttons.contains(&0) {
        (6, 0)
    } else if buttons.contains(&2) {
        (7, 1)
    } else if buttons.contains(&1) {
        (27, 2)
    } else {
        (5, 0)
    }
}

#[cfg(target_os = "macos")]
mod native {
    use super::*;
    use crate::remote::{Monitor, Rect};
    use std::{
        ffi::c_void,
        ptr,
        sync::Mutex,
        time::{Duration, Instant},
    };
    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct Point {
        x: f64,
        y: f64,
    }
    #[repr(C)]
    struct Size {
        width: f64,
        height: f64,
    }
    #[repr(C)]
    struct Bounds {
        origin: Point,
        size: Size,
    }
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGPreflightPostEventAccess() -> bool;
        fn CGRequestPostEventAccess() -> bool;
        fn CGEventSourceCreate(state: i32) -> *mut c_void;
        fn CGEventCreate(source: *mut c_void) -> *mut c_void;
        fn CGEventGetLocation(event: *mut c_void) -> Point;
        fn CGEventCreateMouseEvent(source: *mut c_void, kind: u32, point: Point, button: u32) -> *mut c_void;
        fn CGEventCreateKeyboardEvent(source: *mut c_void, key: u16, down: bool) -> *mut c_void;
        fn CGEventCreateScrollWheelEvent(source: *mut c_void, unit: u32, count: u32, ...) -> *mut c_void;
        fn CGEventSetFlags(event: *mut c_void, flags: u64);
        fn CGEventSetType(event: *mut c_void, kind: u32);
        fn CGEventSetIntegerValueField(event: *mut c_void, field: u32, value: i64);
        fn CGEventPost(tap: u32, event: *mut c_void);
        fn CGGetActiveDisplayList(max: u32, displays: *mut u32, count: *mut u32) -> i32;
        fn CGDisplayBounds(display: u32) -> Bounds;
        fn CGMainDisplayID() -> u32;
        fn CGDisplayCopyDisplayMode(display: u32) -> *mut c_void;
        fn CGDisplayModeGetPixelWidth(mode: *mut c_void) -> usize;
        fn CGDisplayModeGetPixelHeight(mode: *mut c_void) -> usize;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFRelease(value: *const c_void);
    }
    struct Owned(*mut c_void);
    impl Drop for Owned {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    CFRelease(self.0);
                }
            }
        }
    }

    pub fn permission(prompt: bool) -> Result<(), String> {
        // SAFETY: these APIs have no pointer arguments. macOS owns the consent UI.
        if unsafe { CGPreflightPostEventAccess() || (prompt && CGRequestPostEventAccess()) } {
            Ok(())
        } else {
            Err("Allow CoRuby in System Settings > Privacy & Security > Accessibility, then try control again.".into())
        }
    }
    pub fn monitors() -> Vec<Monitor> {
        // SAFETY: count is bounded by the initialized display buffer; returned modes are released.
        unsafe {
            let mut count = 0;
            if CGGetActiveDisplayList(0, ptr::null_mut(), &mut count) != 0 {
                return vec![];
            }
            let mut ids = vec![0; count as usize];
            if CGGetActiveDisplayList(count, ids.as_mut_ptr(), &mut count) != 0 {
                return vec![];
            }
            ids.truncate(count as usize);
            ids.into_iter()
                .map(|id| {
                    let r = CGDisplayBounds(id);
                    let mode = Owned(CGDisplayCopyDisplayMode(id));
                    let pixels = if mode.0.is_null() {
                        None
                    } else {
                        Some((
                            CGDisplayModeGetPixelWidth(mode.0) as u32,
                            CGDisplayModeGetPixelHeight(mode.0) as u32,
                        ))
                    };
                    Monitor {
                        id: id.to_string(),
                        rect: Rect {
                            x: r.origin.x as i32,
                            y: r.origin.y as i32,
                            w: r.size.width as i32,
                            h: r.size.height as i32,
                        },
                        primary: id == CGMainDisplayID(),
                        pixels,
                        capture_id: None,
                    }
                })
                .collect()
        }
    }
    #[derive(Default)]
    struct State {
        buttons: Vec<u8>,
        modifiers: Modifiers,
        click: Option<(u8, Point, Instant, i64)>,
        caps: bool,
    }
    static STATE: Mutex<State> =
        Mutex::new(State { buttons: Vec::new(), modifiers: Modifiers(Vec::new()), click: None, caps: false });
    pub fn send(ops: &[Op]) {
        let mut state = STATE.lock().unwrap();
        // SAFETY: all CG objects are null-checked and released, and point/enum values are bounded
        // by the shared planner. REMOTE remains locked through posting, including stop releases.
        unsafe {
            let source = Owned(CGEventSourceCreate(-1)); // private state, isolated from local keys
            if source.0.is_null() {
                tracing::error!("could not create Quartz event source");
                return;
            }
            let current = Owned(CGEventCreate(source.0));
            let mut point =
                if current.0.is_null() { Point::default() } else { CGEventGetLocation(current.0) };
            for op in ops {
                let event = match *op {
                    Op::MoveTo { x, y } => {
                        point = Point { x: f64::from(x), y: f64::from(y) };
                        let (kind, button) = motion(&state.buttons);
                        CGEventCreateMouseEvent(source.0, kind, point, button)
                    }
                    Op::Button { button, down } => {
                        if down && !state.buttons.contains(&button) {
                            state.buttons.push(button);
                        }
                        if !down {
                            state.buttons.retain(|b| *b != button);
                        }
                        let (kind, native_button) = match (button, down) {
                            (0, true) => (1, 0),
                            (0, false) => (2, 0),
                            (2, true) => (3, 1),
                            (2, false) => (4, 1),
                            (_, true) => (25, 2),
                            (_, false) => (26, 2),
                        };
                        if down {
                            let count = state
                                .click
                                .filter(|(b, p, time, _)| {
                                    *b == button
                                        && time.elapsed() < Duration::from_millis(500)
                                        && (p.x - point.x).abs() < 4.0
                                        && (p.y - point.y).abs() < 4.0
                                })
                                .map_or(1, |c| c.3 + 1);
                            state.click = Some((button, point, Instant::now(), count));
                        }
                        let e = CGEventCreateMouseEvent(source.0, kind, point, native_button);
                        if !e.is_null() {
                            CGEventSetIntegerValueField(e, 1, state.click.map_or(1, |c| c.3));
                        }
                        e
                    }
                    Op::Wheel { vertical, horizontal } => CGEventCreateScrollWheelEvent(
                        source.0,
                        0,
                        2,
                        (f64::from(vertical) / 1.2).round() as i32,
                        (-f64::from(horizontal) / 1.2).round() as i32,
                    ),
                    Op::Key { scan, extended, down } => {
                        let Some(key) = keycode(scan, extended) else {
                            continue;
                        };
                        if key == 57 && down {
                            state.caps = !state.caps;
                        }
                        state.modifiers.update(key, down);
                        let e = CGEventCreateKeyboardEvent(source.0, key, down);
                        if !e.is_null() && (modifier(key) != 0 || key == 57) {
                            CGEventSetType(e, 12);
                        }
                        e
                    }
                };
                let event = Owned(event);
                if !event.0.is_null() {
                    CGEventSetFlags(event.0, state.modifiers.flags() | if state.caps { 1 << 16 } else { 0 });
                    CGEventPost(0, event.0);
                }
            }
        }
    }
}
#[cfg(target_os = "macos")]
pub use native::{monitors, permission, send};

#[cfg(test)]
mod tests {
    use super::*;
    use crate::remote::scan_code;
    #[test]
    fn physical_positions_and_navigation_map_to_mac_keys() {
        for (code, expected) in [
            ("KeyA", 0),
            ("KeyZ", 6),
            ("Digit1", 18),
            ("MetaLeft", 55),
            ("MetaRight", 54),
            ("ControlLeft", 59),
            ("AltRight", 61),
            ("Enter", 36),
            ("NumpadEnter", 76),
            ("Delete", 117),
            ("ArrowLeft", 123),
            ("F12", 111),
        ] {
            let (scan, extended) = scan_code(code).unwrap();
            assert_eq!(keycode(scan, extended), Some(expected), "{code}");
        }
        assert_eq!(keycode(0xffff, false), None);
    }
    #[test]
    fn releasing_one_shift_keeps_the_other_shift_held() {
        let mut m = Modifiers::default();
        m.update(56, true);
        m.update(60, true);
        m.update(55, true);
        m.update(56, false);
        assert_eq!(m.flags(), (1 << 17) | (1 << 20));
        m.update(60, false);
        m.update(55, false);
        assert_eq!(m.flags(), 0);
    }
    #[test]
    fn moves_become_drags_until_release() {
        assert_eq!(motion(&[]), (5, 0));
        assert_eq!(motion(&[0]), (6, 0));
        assert_eq!(motion(&[2]), (7, 1));
        assert_eq!(motion(&[1]), (27, 2));
    }
}
