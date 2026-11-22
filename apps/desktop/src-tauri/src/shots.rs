//! Coding mode's shots (ARCHITECTURE.md, "Coding mode"): grab the window that holds the
//! problem, shrink it, and encode it as lossless WebP for the `screenshot` message.
//!
//! The window is the one in front when it is not ours (the hotkey: the user is in the
//! problem's browser), else the topmost large window that is not ours (the overlay's button:
//! the overlay took the focus). It is captured on its own with `PrintWindow` and
//! `PW_RENDERFULLCONTENT` (xcap), which works for GPU-drawn browsers and leaves out anything on
//! top of it, the overlay included. A window that comes back black (some GPU apps refuse
//! `PrintWindow`), or no window at all, falls back to the monitor: the window's area on it, or
//! the whole monitor under the cursor, with the overlay kept out of capture for the grab.
//!
//! Shots are shrunk to at most 1568 px on the long side (the models shrink anything bigger
//! themselves, so more pixels only cost upload) and encoded losslessly: JPEG blurs small text.
//! A shot over the protocol's 500,000 base64 characters is shrunk further until it fits.

#![cfg_attr(not(any(windows, target_os = "macos")), allow(dead_code, unused_imports))]

use base64::Engine as _;
use image::codecs::{jpeg::JpegEncoder, webp::WebPEncoder};
use image::{ExtendedColorType, ImageEncoder, RgbaImage, imageops::FilterType};
use serde::Serialize;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Instant;

/// The long side a shot is shrunk to.
pub const MAX_SIDE: u32 = 1568;
/// The protocol's limit for one image's base64 (`ScreenImage.data`).
pub const MAX_BASE64: usize = 500_000;
/// Shots in one `screenshot` message.
pub const MAX_SHOTS: usize = 6;
/// A window smaller than this is a toolbar, a notification or a floating video, not a problem.
const MIN_WIDTH: u32 = 400;
const MIN_HEIGHT: u32 = 300;

static NEXT_ID: AtomicU32 = AtomicU32::new(1);

/// Where a shot came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    Window,
    Monitor,
}

/// One encoded shot, waiting in the tray until Solve sends it.
#[derive(Debug, Clone)]
pub struct Shot {
    pub id: u32,
    /// Base64 of the encoded image.
    pub data: String,
    pub media_type: &'static str,
    pub width: u32,
    pub height: u32,
    pub title: Option<String>,
    pub source: Source,
    pub capture_ms: u32,
    pub encode_ms: u32,
}

impl Shot {
    pub fn bytes(&self) -> usize {
        self.data.len() * 3 / 4
    }

    /// What the overlay's tray shows: the image itself as a data URL, its size and timings.
    pub fn view(&self) -> ShotView {
        ShotView {
            id: self.id,
            url: format!("data:{};base64,{}", self.media_type, self.data),
            width: self.width,
            height: self.height,
            title: self.title.clone(),
            bytes: self.bytes(),
            source: self.source,
            capture_ms: self.capture_ms,
            encode_ms: self.encode_ms,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ShotView {
    pub id: u32,
    pub url: String,
    pub width: u32,
    pub height: u32,
    pub title: Option<String>,
    pub bytes: usize,
    pub source: Source,
    pub capture_ms: u32,
    pub encode_ms: u32,
}

/// A top-level window, as the picker sees it.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub pid: u32,
    pub width: u32,
    pub height: u32,
    pub minimized: bool,
    pub focused: bool,
}

/// The window to capture, from the windows in z-order (topmost first): the focused one if it
/// is not ours and large enough, else the topmost that is.
pub fn pick(windows: &[Candidate], own_pid: u32) -> Option<usize> {
    let ok =
        |c: &Candidate| c.pid != own_pid && !c.minimized && c.width >= MIN_WIDTH && c.height >= MIN_HEIGHT;
    windows.iter().position(|c| c.focused && ok(c)).or_else(|| windows.iter().position(ok))
}

/// A size that fits `max` on the long side, keeping the aspect ratio.
pub fn fit(width: u32, height: u32, max: u32) -> (u32, u32) {
    let long = width.max(height);
    if long <= max {
        return (width, height);
    }
    let scale = f64::from(max) / f64::from(long);
    let w = (f64::from(width) * scale).round().max(1.0) as u32;
    let h = (f64::from(height) * scale).round().max(1.0) as u32;
    (w, h)
}

/// Nearly every sampled pixel black: what `PrintWindow` returns for a window it cannot draw.
pub fn mostly_black(img: &RgbaImage) -> bool {
    let (w, h) = img.dimensions();
    if w == 0 || h == 0 {
        return true;
    }
    let (mut dark, mut total) = (0u32, 0u32);
    for y in (0..h).step_by((h / 48).max(1) as usize) {
        for x in (0..w).step_by((w / 48).max(1) as usize) {
            let p = img.get_pixel(x, y).0;
            total += 1;
            if p[0] < 8 && p[1] < 8 && p[2] < 8 {
                dark += 1;
            }
        }
    }
    dark * 100 >= total * 98
}

/// An encoded shot: base64, its size, and its media type.
pub struct Encoded {
    pub data: String,
    pub width: u32,
    pub height: u32,
    pub media_type: &'static str,
}

/// Lossless WebP of the image, shrunk to `MAX_SIDE`; if its base64 is over the limit, lossless
/// once more at 80%, then JPEG at quality 85, shrinking until it fits. Text pages are 30 to
/// 150 KB lossless; only photo-like screens (a video, an image) need the JPEG.
pub fn encode(img: &RgbaImage) -> Result<Encoded, String> {
    let (mut w, mut h) = fit(img.width(), img.height(), MAX_SIDE);
    for attempt in 0..8 {
        let scaled = if (w, h) == img.dimensions() {
            img.clone()
        } else {
            image::imageops::resize(img, w, h, FilterType::CatmullRom)
        };
        let rgb: Vec<u8> = scaled.pixels().flat_map(|p| [p.0[0], p.0[1], p.0[2]]).collect();
        let mut out = Vec::new();
        let media_type = if attempt < 2 {
            WebPEncoder::new_lossless(&mut out)
                .write_image(&rgb, w, h, ExtendedColorType::Rgb8)
                .map_err(|e| format!("could not encode the shot: {e}"))?;
            "image/webp"
        } else {
            JpegEncoder::new_with_quality(&mut out, 85)
                .write_image(&rgb, w, h, ExtendedColorType::Rgb8)
                .map_err(|e| format!("could not encode the shot: {e}"))?;
            "image/jpeg"
        };
        let data = base64::engine::general_purpose::STANDARD.encode(&out);
        if data.len() <= MAX_BASE64 {
            return Ok(Encoded { data, width: w, height: h, media_type });
        }
        if attempt != 1 {
            (w, h) = fit(w, h, (w.max(h) as f64 * 0.8) as u32);
        }
    }
    Err("the shot is too detailed to send, even shrunk".into())
}

fn finish(img: RgbaImage, title: Option<String>, source: Source, started: Instant) -> Result<Shot, String> {
    let captured = Instant::now();
    let Encoded { data, width, height, media_type } = encode(&img)?;
    Ok(Shot {
        id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
        data,
        media_type,
        width,
        height,
        title: title.filter(|t| !t.trim().is_empty()),
        source,
        capture_ms: captured.duration_since(started).as_millis() as u32,
        encode_ms: captured.elapsed().as_millis() as u32,
    })
}

/// Grab the problem's window. `keep_out` is called with true before a monitor grab and false
/// after it, so the app can keep its overlay out of that capture.
#[cfg(windows)]
pub fn grab(own_pid: u32, keep_out: &dyn Fn(bool)) -> Result<Shot, String> {
    use xcap::{Monitor, Window};
    let started = Instant::now();
    let windows = Window::all().map_err(|e| format!("could not list the windows: {e}"))?;
    let candidates: Vec<Candidate> = windows
        .iter()
        .map(|w| Candidate {
            pid: w.pid().unwrap_or(0),
            width: w.width().unwrap_or(0),
            height: w.height().unwrap_or(0),
            minimized: w.is_minimized().unwrap_or(true),
            focused: w.is_focused().unwrap_or(false),
        })
        .collect();
    let monitor_grab = |x: i32, y: i32, area: Option<(i32, i32, u32, u32)>| -> Result<RgbaImage, String> {
        let monitor = Monitor::from_point(x, y).map_err(|e| format!("no monitor there: {e}"))?;
        keep_out(true);
        let img = match area {
            Some((ax, ay, aw, ah)) => {
                let (mx, my) = (monitor.x().unwrap_or(0), monitor.y().unwrap_or(0));
                let (mw, mh) = (monitor.width().unwrap_or(0), monitor.height().unwrap_or(0));
                let left = (ax - mx).clamp(0, mw as i32) as u32;
                let top = (ay - my).clamp(0, mh as i32) as u32;
                let width = aw.min(mw - left);
                let height = ah.min(mh - top);
                monitor.capture_region(left, top, width, height)
            }
            None => monitor.capture_image(),
        };
        keep_out(false);
        img.map_err(|e| format!("could not capture the screen: {e}"))
    };
    if let Some(i) = pick(&candidates, own_pid) {
        let window = &windows[i];
        let title = window.title().ok();
        match window.capture_image() {
            Ok(img) if !mostly_black(&img) => return finish(img, title, Source::Window, started),
            Ok(_) => tracing::info!("coding shot: the window came back black; capturing its area"),
            Err(e) => tracing::info!("coding shot: window capture failed ({e}); capturing its area"),
        }
        let (x, y) = (window.x().unwrap_or(0), window.y().unwrap_or(0));
        let (w, h) = (candidates[i].width, candidates[i].height);
        let img = monitor_grab(x + w as i32 / 2, y + h as i32 / 2, Some((x, y, w, h)))?;
        return finish(img, title, Source::Monitor, started);
    }
    let (x, y) = cursor();
    let img = monitor_grab(x, y, None)?;
    finish(img, None, Source::Monitor, started)
}

#[cfg(windows)]
fn cursor() -> (i32, i32) {
    use windows::Win32::Foundation::POINT;
    use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;
    let mut p = POINT::default();
    // SAFETY: GetCursorPos writes the position into the POINT it is given.
    unsafe {
        let _ = GetCursorPos(&mut p);
    }
    (p.x, p.y)
}

/// Capture only the selected window on macOS; never fall back to unrelated screen content.
#[cfg(target_os = "macos")]
pub fn grab(own_pid: u32, _keep_out: &dyn Fn(bool)) -> Result<Shot, String> {
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
    }
    // SAFETY: these system functions take no pointers and query/request TCC permission.
    if !unsafe { CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() } {
        return Err("Allow CoRuby in System Settings > Privacy & Security > Screen Recording, then reopen the app and try again.".into());
    }
    let started = Instant::now();
    let windows = xcap::Window::all().map_err(|e| format!("could not list the windows: {e}"))?;
    let candidates: Vec<Candidate> = windows
        .iter()
        .map(|w| Candidate {
            pid: w.pid().unwrap_or(0),
            width: w.width().unwrap_or(0),
            height: w.height().unwrap_or(0),
            minimized: w.is_minimized().unwrap_or(true),
            focused: w.is_focused().unwrap_or(false),
        })
        .collect();
    let i = pick(&candidates, own_pid).ok_or("Open the problem window before taking a screenshot.")?;
    let window = &windows[i];
    let img = crate::native_screen::capture_window(window.id().map_err(|e| e.to_string())?)?;
    finish(img, window.title().ok(), Source::Window, started)
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn grab(_own_pid: u32, _keep_out: &dyn Fn(bool)) -> Result<Shot, String> {
    Err("Screen capture is supported on Windows and macOS.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgba;

    fn window(pid: u32, w: u32, h: u32, focused: bool) -> Candidate {
        Candidate { pid, width: w, height: h, minimized: false, focused }
    }

    #[test]
    fn the_focused_window_unless_it_is_ours() {
        let ws = [window(1, 800, 600, false), window(9, 1600, 900, true), window(7, 1600, 900, false)];
        assert_eq!(pick(&ws, 1), Some(1));
        // The overlay's button took the focus: the topmost window that is not ours.
        let ws = [window(1, 800, 600, true), window(1, 1200, 800, false), window(7, 1600, 900, false)];
        assert_eq!(pick(&ws, 1), Some(2));
    }

    #[test]
    fn small_and_minimized_windows_are_skipped() {
        let mut min = window(7, 1600, 900, false);
        min.minimized = true;
        let ws = [window(5, 320, 180, false), min, window(8, 1280, 720, false)];
        assert_eq!(pick(&ws, 1), Some(2));
        assert_eq!(pick(&[window(1, 800, 600, true)], 1), None);
    }

    #[test]
    fn shots_fit_the_long_side() {
        assert_eq!(fit(2560, 1440, 1568), (1568, 882));
        assert_eq!(fit(1080, 1920, 1568), (882, 1568));
        assert_eq!(fit(1200, 800, 1568), (1200, 800));
    }

    #[test]
    fn a_black_window_is_noticed() {
        let black = RgbaImage::from_pixel(200, 100, Rgba([0, 0, 0, 255]));
        assert!(mostly_black(&black));
        let mut page = black.clone();
        for x in 0..200 {
            for y in 0..50 {
                page.put_pixel(x, y, Rgba([250, 250, 250, 255]));
            }
        }
        assert!(!mostly_black(&page));
    }

    #[test]
    fn a_page_encodes_small_and_lossless() {
        // A light page with dark "text" stripes, at 2560 x 1440.
        let img = RgbaImage::from_fn(2560, 1440, |x, y| {
            if y % 24 < 14 && x % 9 < 6 && (x / 300) % 3 != 2 {
                Rgba([30, 30, 30, 255])
            } else {
                Rgba([255, 255, 255, 255])
            }
        });
        let e = encode(&img).unwrap();
        assert_eq!((e.width, e.height), (1568, 882));
        assert_eq!(e.media_type, "image/webp");
        assert!(e.data.len() <= MAX_BASE64);
        let bytes = base64::engine::general_purpose::STANDARD.decode(&e.data).unwrap();
        assert_eq!(&bytes[..4], b"RIFF");
        assert_eq!(&bytes[8..12], b"WEBP");
        let back = image::load_from_memory(&bytes).unwrap();
        assert_eq!((back.width(), back.height()), (1568, 882));
    }

    #[test]
    fn noise_too_big_for_lossless_goes_as_jpeg() {
        let mut seed = 7u32;
        let img = RgbaImage::from_fn(1568, 1568, |_, _| {
            seed = seed.wrapping_mul(1_103_515_245).wrapping_add(12345);
            let v = (seed >> 16) as u8;
            Rgba([v, v.wrapping_mul(3), v.wrapping_mul(7), 255])
        });
        let e = encode(&img).unwrap();
        assert!(e.data.len() <= MAX_BASE64);
        assert_eq!(e.media_type, "image/jpeg");
    }
}

/// A real grab of the window in front, for measuring on a desktop:
/// `cargo test -p copilot-desktop --release shots::real -- --ignored --nocapture`.
#[cfg(all(test, windows))]
mod real {
    #[test]
    #[ignore = "captures the real screen"]
    fn grab_the_window_in_front() {
        for _ in 0..3 {
            let shot = super::grab(std::process::id(), &|_| {}).expect("a shot");
            println!(
                "{:?} {:?}: {}x{} {} {} KB, capture {} ms, encode {} ms",
                shot.source,
                shot.title,
                shot.width,
                shot.height,
                shot.media_type,
                shot.bytes() / 1024,
                shot.capture_ms,
                shot.encode_ms
            );
            assert!(shot.data.len() <= super::MAX_BASE64);
        }
    }
}
