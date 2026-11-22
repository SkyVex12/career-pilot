//! Windows capture uses Ruby's explicit source picker and stop controls, rather than a
//! second WebView2 sharing toolbar. Frames stay in memory and only the newest is retained.
use super::{Frame, Source};
use base64::Engine;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

const LEASE: Duration = Duration::from_secs(10);
const FRAME_INTERVAL: Duration = Duration::from_millis(33);
static NEXT: AtomicU64 = AtomicU64::new(1);
static RUNNING: Mutex<Option<Running>> = Mutex::new(None);

struct Running {
    token: u64,
    owner: String,
    last_read: Instant,
    last_frame: Arc<Mutex<Instant>>,
    cancel: Arc<AtomicBool>,
    latest: Arc<Mutex<Latest>>,
}
#[derive(Default)]
struct Latest {
    sequence: u64,
    width: u32,
    height: u32,
    data: Vec<u8>,
    error: Option<String>,
}
impl Running {
    fn owns(&self, owner: &str, token: u64) -> bool {
        self.token == token && self.owner == owner
    }
    fn expired(&self, now: Instant) -> bool {
        now.saturating_duration_since(self.last_read) > LEASE
            || self.last_frame.lock().map(|last| now.saturating_duration_since(*last) > LEASE).unwrap_or(true)
    }
}

/// The lease cannot depend on capture_image returning: PrintWindow may block in
/// another application's UI thread. This helper never takes the latest-frame lock.
fn expire_current(active: &mut Option<Running>, token: u64, now: Instant) -> bool {
    if !active.as_ref().is_some_and(|running| running.token == token && running.expired(now)) {
        return false;
    }
    if let Some(running) = active.take() {
        running.cancel.store(true, Ordering::Release);
    }
    true
}

fn watch_lease(token: u64) {
    loop {
        std::thread::sleep(Duration::from_secs(2));
        let Ok(mut active) = RUNNING.lock() else { return };
        if !active.as_ref().is_some_and(|running| running.token == token) {
            return;
        }
        if expire_current(&mut active, token, Instant::now()) {
            // Keep the registry locked until input is released so an old capture
            // cannot end a newer share's control after its lease has been replaced.
            crate::remote::end();
            return;
        }
    }
}

pub fn sources() -> Result<Vec<Source>, String> {
    let mut result = Vec::new();
    for monitor in xcap::Monitor::all().map_err(|e| e.to_string())? {
        let (Ok(id), Ok(name), Ok(width), Ok(height)) =
            (monitor.id(), monitor.name(), monitor.width(), monitor.height())
        else {
            continue;
        };
        let title = format!(
            "{} · {} × {}",
            if monitor.is_primary().unwrap_or(false) { "Main display" } else { "External display" },
            width,
            height
        );
        result.push(Source { id, kind: "monitor", title, monitor_id: Some(name) });
    }
    for window in xcap::Window::all().map_err(|e| e.to_string())? {
        if window.pid().ok() == Some(std::process::id()) || window.is_minimized().unwrap_or(true) {
            continue;
        }
        if let (Ok(id), Ok(title)) = (window.id(), window.title())
            && !title.trim().is_empty()
        {
            result.push(Source { id, kind: "window", title, monitor_id: None });
        }
    }
    Ok(result)
}

enum Capture {
    Monitor(xcap::Monitor),
    Window(xcap::Window),
}
impl Capture {
    fn find(id: u32, kind: &str) -> Result<Self, String> {
        match kind {
            "monitor" => xcap::Monitor::all()
                .map_err(|e| e.to_string())?
                .into_iter()
                .find(|m| m.id().ok() == Some(id))
                .map(Self::Monitor),
            "window" => xcap::Window::all()
                .map_err(|e| e.to_string())?
                .into_iter()
                .find(|w| {
                    w.id().ok() == Some(id)
                        && w.pid().ok() != Some(std::process::id())
                        && !w.is_minimized().unwrap_or(true)
                })
                .map(Self::Window),
            _ => return Err("Unknown capture source.".into()),
        }
        .ok_or_else(|| "That display or window is no longer available. Choose another source.".into())
    }
    fn image(&self) -> Result<image::RgbaImage, String> {
        match self {
            Self::Monitor(m) => m.capture_image(),
            Self::Window(w) => w.capture_image(),
        }
        .map_err(|e| e.to_string())
    }
}

fn encode(image: image::RgbaImage) -> Result<(Vec<u8>, u32, u32), String> {
    let mut image = image::DynamicImage::ImageRgba8(image);
    if image.width() > 1920 || image.height() > 1080 {
        image = image.thumbnail(1920, 1080);
    }
    let rgb = image.to_rgb8();
    let (width, height) = rgb.dimensions();
    let mut bytes = Vec::new();
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut bytes, 82)
        .encode(rgb.as_raw(), width, height, image::ExtendedColorType::Rgb8)
        .map_err(|e| e.to_string())?;
    Ok((bytes, width, height))
}

pub fn start(owner: String, id: u32, kind: String) -> Result<u64, String> {
    // Validate before creating the lease. No input permission is granted by capture itself.
    Capture::find(id, &kind)?;
    let mut running = RUNNING.lock().map_err(|_| "Screen capture state is unavailable.")?;
    if running.is_some() {
        return Err("Stop the current screen share before starting another.".into());
    }
    let token = NEXT.fetch_add(1, Ordering::Relaxed);
    let cancel = Arc::new(AtomicBool::new(false));
    let latest = Arc::new(Mutex::new(Latest::default()));
    let last_frame = Arc::new(Mutex::new(Instant::now()));
    *running = Some(Running {
        token,
        owner,
        last_read: Instant::now(),
        last_frame: last_frame.clone(),
        cancel: cancel.clone(),
        latest: latest.clone(),
    });
    let started = std::thread::Builder::new().name("ruby-screen-capture".into()).spawn(move || {
        let capture = Capture::find(id, &kind);
        if let Err(error) = &capture
            && let Ok(mut frame) = latest.lock()
        {
            frame.error = Some(error.clone());
        }
        loop {
            if cancel.load(Ordering::Acquire) {
                return;
            }
            let expired = RUNNING
                .lock()
                .map(|active| match active.as_ref() {
                    Some(active) if active.token == token => active.expired(Instant::now()),
                    _ => true,
                })
                .unwrap_or(true);
            if expired {
                let _ = stop(None, token);
                return;
            }
            let started = Instant::now();
            if let Ok(capture) = &capture {
                let result = capture.image().and_then(encode);
                if cancel.load(Ordering::Acquire) {
                    return;
                }
                if result.is_ok()
                    && let Ok(mut progress) = last_frame.lock()
                {
                    *progress = Instant::now();
                }
                if let Ok(mut frame) = latest.lock() {
                    match result {
                        Ok((data, width, height)) => {
                            frame.sequence += 1;
                            frame.width = width;
                            frame.height = height;
                            frame.data = data;
                            frame.error = None;
                        }
                        Err(error) => {
                            frame.error = Some(format!("Screen sharing stopped: {error}"));
                        }
                    }
                }
            }
            // The renderer reads the newest frame. Nothing queues behind a slow connection.
            std::thread::sleep(FRAME_INTERVAL.saturating_sub(started.elapsed()));
        }
    });
    if let Err(error) = started {
        *running = None;
        return Err(error.to_string());
    }
    if let Err(error) =
        std::thread::Builder::new().name("ruby-screen-lease".into()).spawn(move || watch_lease(token))
    {
        if let Some(active) = running.take() {
            active.cancel.store(true, Ordering::Release);
        }
        return Err(error.to_string());
    }
    Ok(token)
}

pub fn frame(owner: &str, token: u64, after: u64) -> Result<Option<Frame>, String> {
    let mut active = RUNNING.lock().map_err(|_| "Screen capture state is unavailable.")?;
    let running = active.as_mut().filter(|v| v.owns(owner, token)).ok_or("Screen sharing has stopped.")?;
    running.last_read = Instant::now();
    let latest = running.latest.lock().map_err(|_| "Screen frame is unavailable.")?;
    if latest.sequence <= after && latest.error.is_none() {
        return Ok(None);
    }
    Ok(Some(Frame {
        sequence: latest.sequence,
        width: latest.width,
        height: latest.height,
        data: base64::engine::general_purpose::STANDARD.encode(&latest.data),
        error: latest.error.clone(),
    }))
}

pub fn stop(owner: Option<&str>, token: u64) -> Result<(), String> {
    let mut active = RUNNING.lock().map_err(|_| "Screen capture state is unavailable.")?;
    if !active.as_ref().is_some_and(|v| v.token == token && owner.is_none_or(|owner| v.owns(owner, token))) {
        return Ok(());
    }
    if let Some(running) = active.take() {
        running.cancel.store(true, Ordering::Release);
    }
    crate::remote::end();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capture_lease_is_bound_to_the_window_and_generation() {
        let now = Instant::now();
        let active = Running {
            token: 8,
            owner: "overlay".into(),
            last_read: now,
            last_frame: Arc::new(Mutex::new(now)),
            cancel: Arc::new(AtomicBool::new(false)),
            latest: Arc::new(Mutex::new(Latest::default())),
        };
        assert!(active.owns("overlay", 8));
        assert!(!active.owns("main", 8));
        assert!(!active.owns("overlay", 7));
        assert!(!active.expired(now + LEASE));
        assert!(active.expired(now + LEASE + Duration::from_millis(1)));
    }
    #[test]
    fn expired_lease_releases_the_slot_without_waiting_for_the_frame_worker() {
        let now = Instant::now();
        let cancel = Arc::new(AtomicBool::new(false));
        let latest = Arc::new(Mutex::new(Latest::default()));
        let mut active = Some(Running {
            token: 8,
            owner: "overlay".into(),
            last_read: now,
            last_frame: Arc::new(Mutex::new(now)),
            cancel: cancel.clone(),
            latest: latest.clone(),
        });
        // A blocked capture/publisher must not prevent the independent lease from
        // freeing the slot. In particular, expiry must not wait for this mutex.
        let _frame = latest.lock().unwrap();
        assert!(expire_current(&mut active, 8, now + LEASE + Duration::from_millis(1)));
        assert!(active.is_none());
        assert!(cancel.load(Ordering::Acquire));
    }
    #[test]
    fn old_watchdog_cannot_cancel_a_new_share_or_a_renewed_lease() {
        let now = Instant::now();
        let cancel = Arc::new(AtomicBool::new(false));
        let mut active = Some(Running {
            token: 9,
            owner: "main".into(),
            last_read: now,
            last_frame: Arc::new(Mutex::new(now)),
            cancel: cancel.clone(),
            latest: Arc::new(Mutex::new(Latest::default())),
        });
        assert!(!expire_current(&mut active, 8, now + LEASE + Duration::from_secs(2)));
        assert!(!cancel.load(Ordering::Acquire));
        active.as_mut().unwrap().last_read = now + LEASE;
        *active.as_ref().unwrap().last_frame.lock().unwrap() = now + LEASE;
        assert!(!expire_current(&mut active, 9, now + LEASE + Duration::from_secs(2)));
        assert!(active.as_ref().is_some_and(|running| running.token == 9));
        assert!(!cancel.load(Ordering::Acquire));
    }
    #[test]
    fn polling_cannot_keep_a_stalled_capture_or_its_control_lease_alive() {
        let now = Instant::now();
        let cancel = Arc::new(AtomicBool::new(false));
        let mut active = Some(Running {
            token: 10,
            owner: "overlay".into(),
            last_read: now + LEASE,
            last_frame: Arc::new(Mutex::new(now)),
            cancel: cancel.clone(),
            latest: Arc::new(Mutex::new(Latest::default())),
        });
        assert!(expire_current(&mut active, 10, now + LEASE + Duration::from_millis(1)));
        assert!(active.is_none());
        assert!(cancel.load(Ordering::Acquire));
    }
    #[test]
    fn frames_are_valid_bounded_jpegs_with_the_original_aspect_ratio() {
        let pixels = image::RgbaImage::from_pixel(3840, 2160, image::Rgba([20, 40, 60, 255]));
        let (bytes, width, height) = encode(pixels).unwrap();
        assert_eq!((width, height), (1920, 1080));
        let decoded = image::load_from_memory(&bytes).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (width, height));
    }
}
