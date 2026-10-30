//! Per-monitor overlay layout: where the overlay sits, how big, how see-through, and which
//! view, remembered for each monitor and brought back onto a monitor that still exists when
//! the one it was on is unplugged. Pure geometry, so it is tested without a window; the Tauri
//! glue in lib.rs reads the real monitors (their work areas, in physical pixels) and calls in
//! here.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// The faintest the overlay may go, so a stray opacity step can never dim it to invisible with
/// no obvious way back.
pub const MIN_OPACITY: f64 = 0.3;
pub const MAX_OPACITY: f64 = 1.0;
pub const OPACITY_STEP: f64 = 0.1;

/// The overlay's starting size (wide enough for the transcript and suggestions side by side)
/// and its floor (matches the window's min_inner_size in lib.rs),
/// so a saved layout from a larger monitor is shrunk to fit a smaller one but never below this.
pub const DEFAULT_W: u32 = 760;
pub const DEFAULT_H: u32 = 620;
pub const MIN_W: u32 = 320;
pub const MIN_H: u32 = 240;

pub fn clamp_opacity(v: f64) -> f64 {
    if v.is_nan() {
        return MAX_OPACITY;
    }
    v.clamp(MIN_OPACITY, MAX_OPACITY)
}

/// Opacity after `steps` of a tenth each, rounded to a tenth so stepping up then down lands
/// back on the same value and the stored number stays tidy.
pub fn step_opacity(v: f64, steps: i32) -> f64 {
    let stepped = (v / OPACITY_STEP).round() + f64::from(steps);
    clamp_opacity(stepped * OPACITY_STEP)
}

/// What the overlay remembers for one monitor: its frame (physical pixels, the monitor's own
/// coordinates), how see-through it is, and whether it is in compact view.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct MonitorLayout {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
    pub opacity: f64,
    pub compact: bool,
}

/// A monitor that exists right now: its stable key and work area (physical pixels).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MonitorInfo {
    pub key: String,
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
}

/// Where the overlay should be placed, and the monitor key that layout belongs to.
#[derive(Debug, Clone, PartialEq)]
pub struct Placement {
    pub key: String,
    pub layout: MonitorLayout,
}

/// A stable name for a monitor: its OS name when it has one, otherwise its geometry. The same
/// physical monitor keeps the same key across runs and when others are plugged or unplugged.
pub fn monitor_key(name: Option<&str>, x: i32, y: i32, w: u32, h: u32) -> String {
    match name.map(str::trim).filter(|n| !n.is_empty()) {
        Some(n) => n.to_string(),
        None => format!("{w}x{h}@{x},{y}"),
    }
}

impl MonitorInfo {
    pub fn contains(&self, px: i32, py: i32) -> bool {
        px >= self.x && px < self.x + self.w as i32 && py >= self.y && py < self.y + self.h as i32
    }

    /// A default layout centered on this monitor: used the first time the overlay lands here.
    pub fn centered_default(&self) -> MonitorLayout {
        let w = DEFAULT_W.clamp(MIN_W, self.w);
        let h = DEFAULT_H.clamp(MIN_H, self.h);
        MonitorLayout {
            x: self.x + (self.w.saturating_sub(w) / 2) as i32,
            y: self.y + (self.h.saturating_sub(h) / 2) as i32,
            w,
            h,
            opacity: MAX_OPACITY,
            compact: false,
        }
    }
}

/// Fit a (possibly stale) layout onto a monitor that exists now: shrink it to the work area,
/// then nudge it fully inside, and clamp the opacity. The window ends up visible even when the
/// saved frame came from a bigger or differently-placed monitor.
pub fn clamp_layout(mut l: MonitorLayout, mon: &MonitorInfo) -> MonitorLayout {
    l.w = l.w.clamp(MIN_W, mon.w.max(MIN_W));
    l.h = l.h.clamp(MIN_H, mon.h.max(MIN_H));
    let max_x = mon.x + (mon.w.saturating_sub(l.w)) as i32;
    let max_y = mon.y + (mon.h.saturating_sub(l.h)) as i32;
    l.x = l.x.clamp(mon.x, max_x.max(mon.x));
    l.y = l.y.clamp(mon.y, max_y.max(mon.y));
    l.opacity = clamp_opacity(l.opacity);
    l
}

/// Decide where to open the overlay. Prefer the monitor it was last on; failing that, any
/// monitor with a saved layout; failing that, center a default on the preferred-or-first
/// monitor. Returns `None` only when there are no monitors at all.
pub fn place(
    layouts: &BTreeMap<String, MonitorLayout>,
    monitors: &[MonitorInfo],
    prefer: Option<&str>,
) -> Option<Placement> {
    if monitors.is_empty() {
        return None;
    }
    // The order to consider monitors in: the preferred one first, then the rest as given.
    let preferred = prefer.and_then(|p| monitors.iter().find(|m| m.key == p));
    let ordered = preferred.into_iter().chain(monitors.iter().filter(|m| Some(m.key.as_str()) != prefer));

    // The first monitor (preferred first) that already has a saved layout.
    for mon in ordered {
        if let Some(saved) = layouts.get(&mon.key) {
            return Some(Placement { key: mon.key.clone(), layout: clamp_layout(*saved, mon) });
        }
    }

    // Nothing saved for any monitor that exists: center a default on the preferred, else first.
    let mon = preferred.unwrap_or(&monitors[0]);
    Some(Placement { key: mon.key.clone(), layout: mon.centered_default() })
}

/// The monitor a window belongs to right now, by its center: the one that contains the center,
/// else the one whose work area the window overlaps most, else the first. Used to file the
/// window's current frame under the right monitor as it is moved or resized.
pub fn monitor_for_window(monitors: &[MonitorInfo], x: i32, y: i32, w: u32, h: u32) -> Option<usize> {
    if monitors.is_empty() {
        return None;
    }
    let (cx, cy) = (x + w as i32 / 2, y + h as i32 / 2);
    if let Some(i) = monitors.iter().position(|m| m.contains(cx, cy)) {
        return Some(i);
    }
    // No monitor holds the center (it is in a gap or off the edge): most-overlapping, else 0.
    let mut best = 0usize;
    let mut best_area = -1i64;
    for (i, m) in monitors.iter().enumerate() {
        let ox = (x + w as i32).min(m.x + m.w as i32) - x.max(m.x);
        let oy = (y + h as i32).min(m.y + m.h as i32) - y.max(m.y);
        let area = i64::from(ox.max(0)) * i64::from(oy.max(0));
        if area > best_area {
            best_area = area;
            best = i;
        }
    }
    Some(best)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mon(key: &str, x: i32, y: i32, w: u32, h: u32) -> MonitorInfo {
        MonitorInfo { key: key.into(), x, y, w, h }
    }

    #[test]
    fn opacity_is_clamped_and_steps_reverse() {
        assert_eq!(clamp_opacity(2.0), MAX_OPACITY);
        assert_eq!(clamp_opacity(0.0), MIN_OPACITY);
        assert_eq!(clamp_opacity(f64::NAN), MAX_OPACITY);
        // Stepping down then back up returns to the start.
        let down = step_opacity(1.0, -1);
        assert!((down - 0.9).abs() < 1e-9);
        assert!((step_opacity(down, 1) - 1.0).abs() < 1e-9);
        // Never past the floor, however many steps down.
        assert_eq!(step_opacity(0.4, -10), MIN_OPACITY);
    }

    #[test]
    fn monitor_key_prefers_name_then_geometry() {
        assert_eq!(monitor_key(Some("DISPLAY1"), 0, 0, 1920, 1080), "DISPLAY1");
        assert_eq!(monitor_key(Some("  "), -100, 0, 1920, 1080), "1920x1080@-100,0");
        assert_eq!(monitor_key(None, 1920, 0, 2560, 1440), "2560x1440@1920,0");
    }

    #[test]
    fn clamp_fits_a_stale_frame_onto_a_smaller_monitor() {
        let small = mon("B", 0, 0, 1280, 720);
        let stale = MonitorLayout { x: 3000, y: 2000, w: 4000, h: 3000, opacity: 5.0, compact: true };
        let fit = clamp_layout(stale, &small);
        assert!(fit.x >= 0 && fit.y >= 0);
        assert!(fit.x + fit.w as i32 <= 1280 && fit.y + fit.h as i32 <= 720);
        assert_eq!(fit.opacity, MAX_OPACITY);
        assert!(fit.compact, "view is preserved while the frame is fitted");
    }

    #[test]
    fn place_recovers_onto_a_present_monitor_when_the_saved_one_is_gone() {
        // Saved a layout on an external monitor to the right, now unplugged.
        let mut layouts = BTreeMap::new();
        layouts.insert(
            "EXTERNAL".to_string(),
            MonitorLayout { x: 2200, y: 100, w: 440, h: 620, opacity: 0.8, compact: false },
        );
        let only = vec![mon("LAPTOP", 0, 0, 1440, 900)];
        // Prefer the unplugged monitor: it is gone, so the overlay is re-homed and centered.
        let p = place(&layouts, &only, Some("EXTERNAL")).unwrap();
        assert_eq!(p.key, "LAPTOP");
        assert!(p.layout.x >= 0 && p.layout.x + p.layout.w as i32 <= 1440);

        // With that monitor back, its remembered (clamped) layout returns.
        let both = vec![mon("LAPTOP", 0, 0, 1440, 900), mon("EXTERNAL", 1440, 0, 1920, 1080)];
        let p = place(&layouts, &both, Some("EXTERNAL")).unwrap();
        assert_eq!(p.key, "EXTERNAL");
        assert_eq!(p.layout.opacity, 0.8);
    }

    #[test]
    fn place_is_none_only_without_monitors() {
        assert!(place(&BTreeMap::new(), &[], None).is_none());
        let one = vec![mon("A", 0, 0, 1920, 1080)];
        assert!(place(&BTreeMap::new(), &one, None).is_some());
    }

    #[test]
    fn a_window_is_filed_under_the_monitor_holding_its_center() {
        let monitors = vec![mon("LEFT", 0, 0, 1920, 1080), mon("RIGHT", 1920, 0, 1920, 1080)];
        assert_eq!(monitor_for_window(&monitors, 1400, 100, 440, 620), Some(0));
        assert_eq!(monitor_for_window(&monitors, 2000, 100, 440, 620), Some(1));
        // Center off every monitor: falls back to the most-overlapping one, not a panic.
        assert!(monitor_for_window(&monitors, -5000, -5000, 440, 620).is_some());
    }
}
