use std::ffi::c_void;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{HINSTANCE, HMODULE, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::{
    CREATESTRUCTW, CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, FindWindowW,
    GWLP_USERDATA, GetWindowLongPtrW, IsWindowVisible, MSG, PM_REMOVE, PeekMessageW, RegisterClassW,
    SMTO_ABORTIFHUNG, SendMessageTimeoutW, SetWindowLongPtrW, UnregisterClassW, WM_COPYDATA, WM_CREATE,
    WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_POPUP, WS_VISIBLE,
};
use windows::core::PCWSTR;

// Keep this test within the app's existing Windows feature set.
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetModuleHandleW(name: PCWSTR) -> HMODULE;
}

// The native COPYDATASTRUCT ABI; System_DataExchange is not an app dependency.
#[repr(C)]
struct CopyData {
    kind: usize,
    length: u32,
    bytes: *const c_void,
}

type Delivery = (usize, Vec<u8>);

unsafe extern "system" fn receive(window: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    unsafe {
        match message {
            WM_CREATE => {
                let creation = &*(lparam.0 as *const CREATESTRUCTW);
                SetWindowLongPtrW(window, GWLP_USERDATA, creation.lpCreateParams as isize);
                LRESULT(0)
            }
            WM_COPYDATA => {
                let sender = GetWindowLongPtrW(window, GWLP_USERDATA) as *const mpsc::Sender<Delivery>;
                if sender.is_null() || lparam.0 == 0 {
                    return LRESULT(0);
                }
                let data = &*(lparam.0 as *const CopyData);
                if data.bytes.is_null() {
                    return LRESULT(0);
                }
                let payload = std::slice::from_raw_parts(data.bytes.cast::<u8>(), data.length as usize);
                // WM_COPYDATA's buffer is valid only until this callback returns.
                LRESULT(isize::from((*sender).send((data.kind, payload.to_vec())).is_ok()))
            }
            _ => DefWindowProcW(window, message, wparam, lparam),
        }
    }
}

struct Window(HWND);

impl Drop for Window {
    fn drop(&mut self) {
        let _ = unsafe { DestroyWindow(self.0) };
    }
}

struct WindowClass {
    name: Vec<u16>,
    instance: HINSTANCE,
}

impl Drop for WindowClass {
    fn drop(&mut self) {
        let _ = unsafe { UnregisterClassW(PCWSTR(self.name.as_ptr()), Some(self.instance)) };
    }
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

fn create_window(class: &WindowClass, title: &str, sender: &mpsc::Sender<Delivery>) -> Window {
    let title = wide(title);
    Window(unsafe {
        CreateWindowExW(
            WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT,
            PCWSTR(class.name.as_ptr()),
            PCWSTR(title.as_ptr()),
            WS_POPUP | WS_VISIBLE,
            -32000,
            -32000,
            16,
            16,
            None,
            None,
            Some(class.instance),
            Some((sender as *const mpsc::Sender<Delivery>).cast()),
        )
        .expect("create native IPC fixture")
    })
}

struct WindowThread {
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Drop for WindowThread {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[test]
fn hidden_helper_still_receives_second_instance_messages() {
    let identifier = format!("copilot-native-helper-test-{}", uuid::Uuid::now_v7());
    let (ready_sender, ready_receiver) = mpsc::channel();
    let (delivery_sender, delivery_receiver) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    let worker_stop = stop.clone();
    let worker_identifier = identifier.clone();
    let thread = std::thread::spawn(move || {
        let class = WindowClass {
            name: wide(&format!("{worker_identifier}-sic")),
            instance: HINSTANCE(unsafe { GetModuleHandleW(PCWSTR::null()) }.0),
        };
        let definition = WNDCLASSW {
            lpfnWndProc: Some(receive),
            hInstance: class.instance,
            lpszClassName: PCWSTR(class.name.as_ptr()),
            ..Default::default()
        };
        assert_ne!(unsafe { RegisterClassW(&definition) }, 0, "register native IPC fixture");
        // Box the callback data so its address remains stable until both HWNDs are destroyed.
        let sender = Box::new(delivery_sender);
        let helper = create_window(&class, &format!("{worker_identifier}-siw"), &sender);
        let decoy = create_window(&class, &format!("{worker_identifier}-siw-other"), &sender);
        assert!(unsafe { IsWindowVisible(helper.0) }.as_bool());
        assert!(unsafe { IsWindowVisible(decoy.0) }.as_bool());

        // Match production's UI-thread call, then prove that repeating it is harmless.
        super::hide_single_instance(&worker_identifier).unwrap();
        super::hide_single_instance(&worker_identifier).unwrap();
        assert!(!unsafe { IsWindowVisible(helper.0) }.as_bool());
        assert!(unsafe { IsWindowVisible(decoy.0) }.as_bool());
        ready_sender.send(helper.0.0 as isize).unwrap();

        // PeekMessage dispatches cross-thread sent messages as well as queued messages.
        // A deadline also bounds teardown if the test fails before sending its request.
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut message = MSG::default();
        while !worker_stop.load(Ordering::Acquire) && Instant::now() < deadline {
            unsafe {
                while PeekMessageW(&mut message, None, 0, 0, PM_REMOVE).as_bool() {
                    DispatchMessageW(&message);
                }
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        // Native windows drop on their owning thread, before their callback data/class.
    });
    let _fixture = WindowThread { stop, thread: Some(thread) };
    let helper = HWND(
        ready_receiver.recv_timeout(Duration::from_secs(5)).expect("native helper ready") as *mut c_void
    );
    let class = wide(&format!("{identifier}-sic"));
    let title = wide(&format!("{identifier}-siw"));
    assert_eq!(unsafe { FindWindowW(PCWSTR(class.as_ptr()), PCWSTR(title.as_ptr())) }.unwrap(), helper);
    assert!(!unsafe { IsWindowVisible(helper) }.as_bool());

    let payload = b"C:\\test working directory\0copilot.exe\0--background\0argument|with delimiter";
    let data = CopyData { kind: 1543, length: payload.len() as u32, bytes: payload.as_ptr().cast() };
    let mut acknowledgement = 0;
    let delivered = unsafe {
        SendMessageTimeoutW(
            helper,
            WM_COPYDATA,
            WPARAM(0),
            LPARAM(&data as *const CopyData as isize),
            SMTO_ABORTIFHUNG,
            1000,
            Some(&mut acknowledgement),
        )
    };
    assert_ne!(delivered.0, 0, "hidden HWND accepts cross-thread WM_COPYDATA");
    assert_eq!(acknowledgement, 1);
    assert_eq!(
        delivery_receiver.recv_timeout(Duration::from_secs(1)).expect("IPC callback delivered"),
        (1543, payload.to_vec()),
    );
}

#[test]
fn missing_helper_is_reported() {
    let identifier = format!("copilot-missing-helper-test-{}", uuid::Uuid::now_v7());
    assert!(super::hide_single_instance(&identifier).is_err());
}
