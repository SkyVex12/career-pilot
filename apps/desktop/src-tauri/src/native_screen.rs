//! Native desktop screen video, bridged to a canvas MediaStream for the LiveKit room.
//! Only the newest frame is retained; stopped/reloaded webviews cannot leave capture running.
use serde::Serialize;
#[derive(Serialize)]
pub struct Source {
    id: u32,
    kind: &'static str,
    title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    monitor_id: Option<String>,
}
#[derive(Serialize)]
pub struct Frame {
    sequence: u64,
    width: u32,
    height: u32,
    data: String,
    error: Option<String>,
}

#[tauri::command]
pub async fn native_screen_sources() -> Result<Vec<Source>, String> {
    #[cfg(any(windows, target_os = "macos"))]
    return tauri::async_runtime::spawn_blocking(native::sources).await.map_err(|e| e.to_string())?;
    #[cfg(not(any(windows, target_os = "macos")))]
    Err("Native screen capture is unavailable on this platform.".into())
}
#[tauri::command]
pub async fn native_screen_start(window: tauri::WebviewWindow, id: u32, kind: String) -> Result<u64, String> {
    #[cfg(any(windows, target_os = "macos"))]
    return tauri::async_runtime::spawn_blocking(move || native::start(window.label().to_owned(), id, kind))
        .await
        .map_err(|e| e.to_string())?;
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = (window, id, kind);
        Err("Native screen capture is unavailable on this platform.".into())
    }
}
#[tauri::command]
pub fn native_screen_frame(
    window: tauri::WebviewWindow,
    token: u64,
    after: u64,
) -> Result<Option<Frame>, String> {
    #[cfg(any(windows, target_os = "macos"))]
    return native::frame(window.label(), token, after);
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = (window, token, after);
        Err("Native screen capture is unavailable on this platform.".into())
    }
}
#[tauri::command]
pub async fn native_screen_stop(window: tauri::WebviewWindow, token: u64) -> Result<(), String> {
    #[cfg(any(windows, target_os = "macos"))]
    return tauri::async_runtime::spawn_blocking(move || native::stop(Some(window.label()), token))
        .await
        .map_err(|e| e.to_string())?;
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = (window, token);
        Ok(())
    }
}

#[tauri::command]
pub async fn native_screen_quality(
    window: tauri::WebviewWindow,
    token: u64,
    height: u32,
) -> Result<(), String> {
    if ![720, 1080, 1440, 2160].contains(&height) {
        return Err("Unsupported screen resolution.".into());
    }
    #[cfg(any(windows, target_os = "macos"))]
    return tauri::async_runtime::spawn_blocking(move || native::quality(window.label(), token, height))
        .await
        .map_err(|e| e.to_string())?;
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = (window, token);
        Err("Native screen capture is unavailable on this platform.".into())
    }
}

#[cfg(windows)]
#[path = "native_screen_windows.rs"]
mod native;

#[cfg(target_os = "macos")]
mod native {
    use super::*;
    use base64::Engine;
    use std::{
        ffi::{CStr, c_char, c_void},
        sync::{
            Mutex,
            atomic::{AtomicU64, Ordering},
        },
        time::{Duration, Instant},
    };
    unsafe extern "C" {
        fn copilot_screen_start(
            id: u32,
            window: bool,
            token: u64,
            frame: extern "C" fn(u64, *const u8, usize, u32, u32),
            error: extern "C" fn(u64, *const c_char),
        ) -> *mut c_void;
        fn copilot_screen_stop(handle: *mut c_void);
        fn copilot_screen_quality(handle: *mut c_void, height: u32) -> bool;
    }
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
    }
    struct Running {
        handle: usize,
        token: u64,
        owner: String,
        last_read: Instant,
    }
    struct Latest {
        token: u64,
        sequence: u64,
        width: u32,
        height: u32,
        data: Vec<u8>,
        error: Option<String>,
    }
    static RUNNING: Mutex<Option<Running>> = Mutex::new(None);
    static LATEST: Mutex<Option<Latest>> = Mutex::new(None);
    static NEXT: AtomicU64 = AtomicU64::new(1);
    extern "C" fn received(token: u64, data: *const u8, len: usize, width: u32, height: u32) {
        if data.is_null() || len > 8_000_000 {
            return;
        }
        let Ok(mut latest) = LATEST.lock() else {
            return;
        };
        if let Some(value) = latest.as_mut().filter(|v| v.token == token) {
            // SAFETY: the bridge holds NSData alive for this callback and supplies its byte length.
            value.data = unsafe { std::slice::from_raw_parts(data, len) }.to_vec();
            value.width = width;
            value.height = height;
            value.sequence += 1;
        }
    }
    extern "C" fn failed(token: u64, error: *const c_char) {
        if error.is_null() {
            return;
        }
        let Ok(mut latest) = LATEST.lock() else {
            return;
        };
        if let Some(value) = latest.as_mut().filter(|v| v.token == token) {
            value.error = Some(unsafe { CStr::from_ptr(error) }.to_string_lossy().into_owned());
        }
    }
    pub fn sources() -> Result<Vec<Source>, String> {
        if !unsafe { CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() } {
            return Err("Allow CoRuby in System Settings > Privacy & Security > Screen Recording, then reopen the app.".into());
        }
        let mut sources: Vec<Source> = crate::remote_macos::monitors()
            .into_iter()
            .filter_map(|m| {
                Some(Source {
                    id: m.id.parse().ok()?,
                    kind: "monitor",
                    monitor_id: Some(m.id.clone()),
                    title: format!(
                        "{}display ({} x {})",
                        if m.primary { "Main " } else { "External " },
                        m.rect.w,
                        m.rect.h
                    ),
                })
            })
            .collect();
        for w in xcap::Window::all().map_err(|e| e.to_string())? {
            if w.pid().ok() == Some(std::process::id()) || w.is_minimized().unwrap_or(true) {
                continue;
            }
            if let (Ok(id), Ok(title)) = (w.id(), w.title()) {
                if !title.is_empty() {
                    sources.push(Source { id, kind: "window", title, monitor_id: None });
                }
            }
        }
        Ok(sources)
    }
    pub fn start(owner: String, id: u32, kind: String) -> Result<u64, String> {
        if kind != "window" && kind != "monitor" {
            return Err("Unknown capture source.".into());
        }
        let mut running = RUNNING.lock().unwrap();
        if running.is_some() {
            return Err("Stop the current screen share before starting another.".into());
        }
        let token = NEXT.fetch_add(1, Ordering::Relaxed);
        *LATEST.lock().unwrap() =
            Some(Latest { token, sequence: 0, width: 0, height: 0, data: vec![], error: None });
        let handle = unsafe { copilot_screen_start(id, kind == "window", token, received, failed) };
        if handle.is_null() {
            let error = LATEST
                .lock()
                .unwrap()
                .take()
                .and_then(|v| v.error)
                .unwrap_or("Could not start screen capture.".into());
            return Err(error);
        }
        *running = Some(Running { handle: handle as usize, token, owner, last_read: Instant::now() });
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(Duration::from_secs(2));
                let expired = {
                    let running = RUNNING.lock().unwrap();
                    match running.as_ref().filter(|v| v.token == token) {
                        Some(v) => v.last_read.elapsed() > Duration::from_secs(10),
                        None => return,
                    }
                };
                if expired {
                    let _ = stop(None, token);
                    return;
                }
            }
        });
        Ok(token)
    }
    pub fn frame(owner: &str, token: u64, after: u64) -> Result<Option<Frame>, String> {
        let mut running = RUNNING.lock().unwrap();
        let running = running
            .as_mut()
            .filter(|r| r.token == token && r.owner == owner)
            .ok_or("Screen sharing has stopped.")?;
        running.last_read = Instant::now();
        let latest = LATEST.lock().unwrap();
        let Some(value) = latest.as_ref().filter(|v| v.sequence > after || v.error.is_some()) else {
            return Ok(None);
        };
        Ok(Some(Frame {
            sequence: value.sequence,
            width: value.width,
            height: value.height,
            data: base64::engine::general_purpose::STANDARD.encode(&value.data),
            error: value.error.clone(),
        }))
    }
    pub fn quality(owner: &str, token: u64, height: u32) -> Result<(), String> {
        let running = RUNNING.lock().map_err(|_| "Screen capture state is unavailable.")?;
        let active = running
            .as_ref()
            .filter(|v| v.token == token && v.owner == owner)
            .ok_or("Screen sharing has stopped.")?;
        if unsafe { copilot_screen_quality(active.handle as *mut c_void, height) } {
            Ok(())
        } else {
            Err("Could not change screen capture resolution.".into())
        }
    }
    pub fn stop(owner: Option<&str>, token: u64) -> Result<(), String> {
        let mut running = RUNNING.lock().unwrap();
        if !running.as_ref().is_some_and(|r| r.token == token && owner.is_none_or(|o| o == r.owner)) {
            return Ok(());
        }
        let old = running.take().unwrap();
        *LATEST.lock().unwrap() = None;
        unsafe {
            copilot_screen_stop(old.handle as *mut c_void);
        }
        crate::remote::end();
        Ok(())
    }
}

#[cfg(target_os = "macos")]
pub fn capture_window(id: u32) -> Result<image::RgbaImage, String> {
    use std::ffi::{CStr, c_char, c_void};
    unsafe extern "C" {
        fn copilot_window_snapshot(
            id: u32,
            context: *mut c_void,
            callback: extern "C" fn(*mut c_void, *const u8, usize, *const c_char),
        );
    }
    extern "C" fn receive(context: *mut c_void, bytes: *const u8, len: usize, error: *const c_char) {
        // SAFETY: the bridge calls once on this worker before returning, while result is live.
        // Its async Apple completions never retain this context. Bytes live for the call.
        unsafe {
            let result = &mut *(context as *mut Result<Vec<u8>, String>);
            *result = if !error.is_null() {
                Err(CStr::from_ptr(error).to_string_lossy().into_owned())
            } else if bytes.is_null() || len > 32_000_000 {
                Err("Invalid screenshot data.".into())
            } else {
                Ok(std::slice::from_raw_parts(bytes, len).to_vec())
            };
        }
    }
    let mut result: Result<Vec<u8>, String> = Err("No screenshot arrived.".into());
    unsafe {
        copilot_window_snapshot(id, &mut result as *mut _ as *mut c_void, receive);
    }
    image::load_from_memory(&result?).map(|image| image.into_rgba8()).map_err(|e| e.to_string())
}
