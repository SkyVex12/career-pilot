//! macOS 13-compatible evidence and system notification bridge. No screen/audio capture.
use super::detection::{Evidence, Suggestion};
use objc2::{
    AnyThread, define_class, msg_send,
    rc::Retained,
    runtime::{Bool, ProtocolObject},
};
use objc2_app_kit::NSWorkspace;
use objc2_foundation::{NSArray, NSError, NSObject, NSObjectProtocol, NSString};
use objc2_user_notifications::*;
use std::{
    collections::BTreeSet,
    ffi::{CString, c_void},
    sync::{Mutex, Once, OnceLock},
    time::{Duration, Instant},
};

type CF = *const c_void;
#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
    fn AXIsProcessTrusted() -> bool;
    fn AXUIElementCreateApplication(pid: i32) -> CF;
    fn AXUIElementCopyAttributeValue(element: CF, attribute: CF, value: *mut CF) -> i32;
    fn AXUIElementSetMessagingTimeout(element: CF, seconds: f32) -> i32;
}
#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFRetain(value: CF) -> CF;
    fn CFRelease(value: CF);
    fn CFGetTypeID(value: CF) -> usize;
    fn CFStringGetTypeID() -> usize;
    fn CFArrayGetTypeID() -> usize;
    fn CFStringCreateWithCString(allocator: CF, text: *const i8, encoding: u32) -> CF;
    fn CFStringGetCString(value: CF, buffer: *mut i8, size: isize, encoding: u32) -> bool;
    fn CFArrayGetCount(value: CF) -> isize;
    fn CFArrayGetValueAtIndex(value: CF, index: isize) -> CF;
}
#[repr(C)]
struct Address {
    selector: u32,
    scope: u32,
    element: u32,
}
#[link(name = "CoreAudio", kind = "framework")]
unsafe extern "C" {
    fn AudioObjectGetPropertyData(
        object: u32,
        address: *const Address,
        qualifier_size: u32,
        qualifier: CF,
        size: *mut u32,
        data: *mut c_void,
    ) -> i32;
}
struct Owned(CF);
impl Drop for Owned {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { CFRelease(self.0) }
        }
    }
}
fn attribute(element: CF, name: &str) -> Option<Owned> {
    unsafe {
        let name = CString::new(name).ok()?;
        let key = Owned(CFStringCreateWithCString(std::ptr::null(), name.as_ptr(), 0x08000100));
        if key.0.is_null() {
            return None;
        }
        let mut value = std::ptr::null();
        (AXUIElementCopyAttributeValue(element, key.0, &mut value) == 0 && !value.is_null())
            .then_some(Owned(value))
    }
}
fn text(value: CF) -> Option<String> {
    unsafe {
        if CFGetTypeID(value) != CFStringGetTypeID() {
            return None;
        }
        let mut bytes = [0i8; 512];
        if !CFStringGetCString(value, bytes.as_mut_ptr(), 512, 0x08000100) {
            return None;
        }
        Some(std::ffi::CStr::from_ptr(bytes.as_ptr()).to_string_lossy().into_owned())
    }
}
fn property(object: u32, selector: [u8; 4]) -> Option<u32> {
    unsafe {
        let address = Address {
            selector: u32::from_be_bytes(selector),
            scope: u32::from_be_bytes(*b"glob"),
            element: 0,
        };
        let mut size = 4;
        let mut value = 0u32;
        (AudioObjectGetPropertyData(
            object,
            &address,
            0,
            std::ptr::null(),
            &mut size,
            (&mut value as *mut u32).cast(),
        ) == 0)
            .then_some(value)
    }
}
pub(super) fn trusted() -> bool {
    unsafe { AXIsProcessTrusted() }
}
pub(super) fn request_accessibility() -> Result<bool, String> {
    std::process::Command::new("/usr/bin/open")
        .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        .status()
        .map_err(|e| e.to_string())?;
    Ok(trusted())
}
fn application(bundle: &str) -> Option<&'static str> {
    match bundle {
        "us.zoom.xos" => Some("Zoom"),
        "com.microsoft.teams" | "com.microsoft.teams2" => Some("Teams"),
        "com.tinyspeck.slackmacgap" => Some("Slack"),
        "com.hnc.Discord" => Some("Discord"),
        "net.whatsapp.WhatsApp" => Some("WhatsApp"),
        "com.apple.FaceTime" => Some("FaceTime"),
        "com.apple.Safari"
        | "com.google.Chrome"
        | "com.microsoft.edgemac"
        | "org.mozilla.firefox"
        | "com.brave.Browser" => Some("Browser"),
        _ => None,
    }
}
pub(super) fn sample(tracking: bool) -> Result<(Vec<Evidence>, bool), String> {
    if !trusted() {
        return Err("Enable Accessibility in macOS System Settings to inspect call controls".into());
    }
    let microphone = property(1, *b"dIn ").and_then(|id| property(id, *b"gone")).is_some_and(|v| v != 0);
    if !microphone && !tracking {
        return Ok((vec![], true));
    }
    let start = Instant::now();
    let mut evidence = Vec::new();
    let applications = NSWorkspace::sharedWorkspace().runningApplications();
    for index in 0..applications.count().min(128) {
        let running = applications.objectAtIndex(index);
        let pid = running.processIdentifier();
        if pid <= 0 || pid as u32 == std::process::id() {
            continue;
        }
        let Some(bundle) = running.bundleIdentifier() else {
            continue;
        };
        let Some(app) = application(&bundle.to_string()) else {
            continue;
        };
        let element = Owned(unsafe { AXUIElementCreateApplication(pid) });
        if element.0.is_null() {
            continue;
        }
        unsafe {
            AXUIElementSetMessagingTimeout(element.0, 0.2);
        }
        let mut stack = vec![(element, 0)];
        let mut names = BTreeSet::new();
        let mut count = 0;
        while let Some((element, depth)) = stack.pop() {
            count += 1;
            if count > 512 || start.elapsed() > Duration::from_secs(3) {
                break;
            }
            if attribute(element.0, "AXRole").and_then(|v| text(v.0)).as_deref() == Some("AXButton") {
                for name in ["AXTitle", "AXDescription"] {
                    if let Some(value) = attribute(element.0, name).and_then(|v| text(v.0)) {
                        names.insert(value.split(" (").next().unwrap_or("").trim().to_lowercase());
                    }
                }
            }
            if depth < 12 {
                if let Some(children) = attribute(element.0, "AXChildren") {
                    unsafe {
                        if CFGetTypeID(children.0) == CFArrayGetTypeID() {
                            for index in 0..CFArrayGetCount(children.0).min(128) {
                                let child = CFArrayGetValueAtIndex(children.0, index);
                                if !child.is_null() {
                                    stack.push((Owned(CFRetain(child)), depth + 1));
                                }
                            }
                        }
                    }
                }
            }
        }
        if super::detection::call_controls(app, &names) {
            evidence.push(Evidence { key: format!("{pid}:{app}"), app: app.into(), audio: microphone });
        }
    }
    // Notifications are delivered through UserNotifications, so macOS owns Focus/DND suppression.
    Ok((evidence, true))
}

static APP: OnceLock<tauri::AppHandle> = OnceLock::new();
static INIT: Once = Once::new();
static LAST: Mutex<Option<String>> = Mutex::new(None);
define_class!(
    #[unsafe(super=NSObject)]
    #[name = "RubyCallNotificationDelegate"]
    struct NotificationDelegate;
    unsafe impl NSObjectProtocol for NotificationDelegate {}
    unsafe impl UNUserNotificationCenterDelegate for NotificationDelegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            done: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            done.call((UNNotificationPresentationOptions::Banner | UNNotificationPresentationOptions::List,));
        }
        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn received(
            &self,
            _center: &UNUserNotificationCenter,
            response: &UNNotificationResponse,
            done: &block2::DynBlock<dyn Fn()>,
        ) {
            let id = response.notification().request().identifier().to_string();
            if let Some(app) = APP.get() {
                let _ = super::background_respond(app.clone(), id, true);
            }
            done.call(());
        }
    }
);
pub(super) fn setup(app: tauri::AppHandle) {
    let _ = APP.set(app);
    INIT.call_once(|| {
        let delegate: Retained<NotificationDelegate> =
            unsafe { msg_send![NotificationDelegate::alloc(), init] };
        UNUserNotificationCenter::currentNotificationCenter()
            .setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        // Notification center holds a weak delegate. Keep one instance for app lifetime.
        std::mem::forget(delegate);
    });
}
pub(super) fn request_notifications() -> Result<bool, String> {
    let (tx, rx) = std::sync::mpsc::channel();
    let block = block2::RcBlock::new(move |granted: Bool, _error: *mut NSError| {
        let _ = tx.send(granted.as_bool());
    });
    UNUserNotificationCenter::currentNotificationCenter()
        .requestAuthorizationWithOptions_completionHandler(UNAuthorizationOptions::Alert, &block);
    rx.recv_timeout(Duration::from_secs(60)).map_err(|_| "Notification permission has not completed".into())
}
pub(super) fn notify(suggestion: Option<&Suggestion>) {
    let center = UNUserNotificationCenter::currentNotificationCenter();
    if let Ok(mut last) = LAST.lock() {
        if let Some(id) = last.take() {
            let id = NSString::from_str(&id);
            let ids = NSArray::from_slice(&[&*id]);
            center.removeDeliveredNotificationsWithIdentifiers(&ids);
            center.removePendingNotificationRequestsWithIdentifiers(&ids);
        }
        if let Some(s) = suggestion {
            let content = UNMutableNotificationContent::new();
            content.setTitle(&NSString::from_str(if s.kind == "end" {
                "Call may have ended"
            } else {
                "Call detected"
            }));
            content.setBody(&NSString::from_str(if s.kind == "end" {
                "Click to review ending Copilot."
            } else {
                "Click to review call setup in Copilot. Audio starts only after confirmation."
            }));
            let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
                &NSString::from_str(&s.id),
                &content,
                None,
            );
            center.addNotificationRequest_withCompletionHandler(&request, None);
            *last = Some(s.id.clone());
        }
    }
}
