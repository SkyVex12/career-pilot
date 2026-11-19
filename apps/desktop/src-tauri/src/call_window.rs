//! The workspace and live overlay change visibility together at call boundaries.

use std::sync::atomic::Ordering;

use tauri::{AppHandle, Emitter, Manager};

use crate::{AppState, OPEN_CALL_EVENT, Res, apply_click_through, workspace_window};

trait Windows {
    fn show_overlay(&self) -> Res<()>;
    fn hide_overlay(&self) -> Res<()>;
    fn show_main(&self) -> Res<()>;
    fn hide_main(&self) -> Res<()>;
}

/// Never hide the only usable window until its replacement has appeared. A window-system
/// failure must not leave a live microphone with no controls the user can reach.
fn enter(windows: &impl Windows) -> Res<()> {
    if let Err(error) = windows.show_overlay().and_then(|()| windows.hide_main()) {
        let _ = windows.show_main();
        return Err(error);
    }
    Ok(())
}

fn leave(windows: &impl Windows) -> Res<()> {
    windows.show_main()?;
    windows.hide_overlay()
}

struct DesktopWindows<'a>(&'a AppHandle);

impl Windows for DesktopWindows<'_> {
    fn show_overlay(&self) -> Res<()> {
        let window = self.0.get_webview_window("overlay").ok_or("no overlay window")?;
        window.set_ignore_cursor_events(false).map_err(|error| error.to_string())?;
        self.0.state::<AppState>().overlay.click_through.store(false, Ordering::SeqCst);
        window.show().map_err(|error| error.to_string())?;
        if window.is_minimized().map_err(|error| error.to_string())? {
            window.unminimize().map_err(|error| error.to_string())?;
        }
        if self.0.state::<AppState>().overlay.temporary_taskbar.swap(false, Ordering::SeqCst) {
            let _ = window.set_skip_taskbar(true);
        }
        apply_click_through(self.0, false);
        let _ = window.set_focus();
        Ok(())
    }

    fn hide_overlay(&self) -> Res<()> {
        let window = self.0.get_webview_window("overlay").ok_or("no overlay window")?;
        window.hide().map_err(|error| error.to_string())?;
        if self.0.state::<AppState>().overlay.temporary_taskbar.swap(false, Ordering::SeqCst) {
            let _ = window.set_skip_taskbar(true);
        }
        Ok(())
    }

    fn show_main(&self) -> Res<()> {
        let window = self.0.get_webview_window("main").ok_or("no main window")?;
        workspace_window::clear_click_through(self.0)?;
        let _ = self.0.emit_to("main", OPEN_CALL_EVENT, ());
        window.show().map_err(|error| error.to_string())?;
        if window.is_minimized().map_err(|error| error.to_string())? {
            window.unminimize().map_err(|error| error.to_string())?;
        }
        let _ = window.set_focus();
        Ok(())
    }

    fn hide_main(&self) -> Res<()> {
        self.0.get_webview_window("main").ok_or("no main window")?.hide().map_err(|error| error.to_string())
    }
}

/// Call only with application locks released, on the native UI thread.
pub(crate) fn started(app: &AppHandle) -> Res<()> {
    enter(&DesktopWindows(app))
}

/// Every terminal stop, including a rejected or failed connection, brings setup back.
pub(crate) fn stopped(app: &AppHandle) -> Res<()> {
    leave(&DesktopWindows(app))
}

pub(crate) fn show_main(app: &AppHandle) -> Res<()> {
    DesktopWindows(app).show_main()
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};

    use super::*;

    struct FakeWindows {
        main_visible: Cell<bool>,
        overlay_visible: Cell<bool>,
        fail: Option<&'static str>,
        calls: RefCell<Vec<&'static str>>,
    }

    impl FakeWindows {
        fn new(main: bool, overlay: bool, fail: Option<&'static str>) -> Self {
            Self {
                main_visible: Cell::new(main),
                overlay_visible: Cell::new(overlay),
                fail,
                calls: RefCell::new(vec![]),
            }
        }

        fn apply(&self, operation: &'static str, visible: &Cell<bool>, value: bool) -> Res<()> {
            self.calls.borrow_mut().push(operation);
            if self.fail == Some(operation) {
                return Err(operation.into());
            }
            visible.set(value);
            assert!(self.main_visible.get() || self.overlay_visible.get(), "both windows were hidden");
            Ok(())
        }
    }

    impl Windows for FakeWindows {
        fn show_overlay(&self) -> Res<()> {
            self.apply("show overlay", &self.overlay_visible, true)
        }
        fn hide_overlay(&self) -> Res<()> {
            self.apply("hide overlay", &self.overlay_visible, false)
        }
        fn show_main(&self) -> Res<()> {
            self.apply("show main", &self.main_visible, true)
        }
        fn hide_main(&self) -> Res<()> {
            self.apply("hide main", &self.main_visible, false)
        }
    }

    #[test]
    fn starting_shows_the_overlay_before_hiding_setup() {
        let windows = FakeWindows::new(true, false, None);
        enter(&windows).unwrap();
        assert!(!windows.main_visible.get());
        assert!(windows.overlay_visible.get());
        assert_eq!(*windows.calls.borrow(), ["show overlay", "hide main"]);
    }

    #[test]
    fn failed_overlay_presentation_keeps_setup_reachable() {
        let windows = FakeWindows::new(true, false, Some("show overlay"));
        assert!(enter(&windows).is_err());
        assert!(windows.main_visible.get());
        assert_eq!(*windows.calls.borrow(), ["show overlay", "show main"]);
    }

    #[test]
    fn failed_setup_hide_keeps_the_live_controls_available() {
        let windows = FakeWindows::new(true, false, Some("hide main"));
        assert!(enter(&windows).is_err());
        assert!(windows.main_visible.get() && windows.overlay_visible.get());
    }

    #[test]
    fn terminal_stop_returns_to_setup_before_hiding_the_overlay() {
        let windows = FakeWindows::new(false, true, None);
        leave(&windows).unwrap();
        assert!(windows.main_visible.get());
        assert!(!windows.overlay_visible.get());
        assert_eq!(*windows.calls.borrow(), ["show main", "hide overlay"]);
    }

    #[test]
    fn failed_main_restore_does_not_hide_the_overlay() {
        let windows = FakeWindows::new(false, true, Some("show main"));
        assert!(leave(&windows).is_err());
        assert!(windows.overlay_visible.get());
        assert_eq!(*windows.calls.borrow(), ["show main"]);
    }
}
