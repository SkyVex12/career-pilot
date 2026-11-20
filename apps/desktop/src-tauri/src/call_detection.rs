//! Local call evidence and lifecycle. No audio capture, source content retention or networking.
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct Suggestion {
    pub id: String,
    pub platform: String,
    pub kind: &'static str,
}
#[derive(Clone, Debug)]
pub(super) struct Evidence {
    pub key: String,
    pub app: String,
    pub audio: bool,
}
#[derive(Clone, Debug, Default, Serialize)]
pub(crate) struct DetectionDiagnostics {
    pub microphone_active: Option<bool>,
    pub microphone_apps: Vec<String>,
    pub inspected_windows: u32,
    pub matched_calls: Vec<String>,
    pub inspection_limited: bool,
    pub notifications_allowed: bool,
}
#[derive(Default)]
pub(super) struct DetectionSample {
    pub evidence: Vec<Evidence>,
    pub notifications: bool,
    pub diagnostics: DetectionDiagnostics,
}
#[derive(Default)]
struct Tracked {
    first: u64,
    last: u64,
    offered: bool,
    accepted: bool,
    end_offered: bool,
}
#[derive(Default)]
pub(super) struct Detector {
    calls: BTreeMap<String, Tracked>,
    pub pending: Option<Suggestion>,
}
impl Detector {
    pub fn suppress_apps(&mut self, disabled: &[String]) {
        let excluded = |key: &str| disabled.iter().any(|app| key.ends_with(&format!(":{app}")));
        if self.pending.as_ref().is_some_and(|s| excluded(&s.id)) {
            self.pending = None;
        }
        for (key, call) in &mut self.calls {
            if excluded(key) {
                call.offered = true;
                call.end_offered = true;
            }
        }
    }
    pub fn tracking(&self) -> bool {
        !self.calls.is_empty()
    }
    pub fn defer_pending(&mut self) {
        if let Some(pending) = self.pending.take()
            && let Some(call) = self.calls.get_mut(&pending.id)
        {
            // OS suppression or a temporary pause is not a dismissal. Only retry
            // unanswered suggestions; a user-dismissed call stays suppressed.
            if pending.kind == "start" {
                call.offered = false;
            } else {
                call.end_offered = false;
            }
        }
    }
    pub fn tick(&mut self, now: u64, evidence: &[Evidence], allowed: bool, active: bool) {
        if !allowed {
            self.defer_pending();
        }
        for e in evidence {
            if !self.calls.contains_key(&e.key) && !e.audio {
                continue;
            }
            let c = self.calls.entry(e.key.clone()).or_insert(Tracked {
                first: now,
                last: now,
                ..Default::default()
            });
            c.last = now;
            if active && !c.accepted {
                c.offered = true;
            }
            if allowed && !active && !c.offered && now.saturating_sub(c.first) >= 5 && self.pending.is_none()
            {
                c.offered = true;
                self.pending = Some(Suggestion { id: e.key.clone(), platform: e.app.clone(), kind: "start" });
            }
        }
        for (id, c) in &mut self.calls {
            if now.saturating_sub(c.last) >= 120
                && c.accepted
                && active
                && !c.end_offered
                && allowed
                && self.pending.is_none()
            {
                c.end_offered = true;
                self.pending = Some(Suggestion { id: id.clone(), platform: "Call".into(), kind: "end" });
            }
        }
        if self.pending.as_ref().is_some_and(|s| {
            s.kind == "start"
                && (active || self.calls.get(&s.id).is_none_or(|c| now.saturating_sub(c.last) >= 120))
        }) {
            self.pending = None;
        }
        self.calls.retain(|_, c| now.saturating_sub(c.last) < 300 || (c.accepted && active));
    }
    pub fn respond(&mut self, id: &str, accept: bool) -> Option<Suggestion> {
        let s = self.pending.as_ref()?;
        if s.id != id {
            return None;
        }
        let s = self.pending.take()?;
        if let Some(c) = self.calls.get_mut(id)
            && s.kind == "start"
        {
            c.accepted = accept;
        }
        Some(s)
    }
}

fn app_name(exe: &str) -> Option<&'static str> {
    match exe.to_ascii_lowercase().as_str() {
        "zoom.exe" => Some("Zoom"),
        "ms-teams.exe" | "teams.exe" => Some("Teams"),
        "slack.exe" => Some("Slack"),
        "discord.exe" => Some("Discord"),
        "whatsapp.exe" => Some("WhatsApp"),
        "chrome.exe" | "msedge.exe" | "firefox.exe" | "brave.exe" => Some("Browser"),
        "hd-player.exe" => Some("BlueStacks"),
        "dnplayer.exe" | "ldplayer.exe" => Some("LDPlayer"),
        "phoneexperiencehost.exe" => Some("Phone Link"),
        _ => None,
    }
}
/// Conservative English accessibility-label adapters. Titles are never evidence.
pub(super) fn call_controls(app: &str, names: &BTreeSet<String>) -> bool {
    let has = |choices: &[&str]| choices.iter().any(|v| names.contains(*v));
    let mic = has(&[
        "mute",
        "unmute",
        "mute microphone",
        "unmute microphone",
        "turn off microphone",
        "turn on microphone",
        "mute my audio",
        "unmute my audio",
    ]);
    let end = match app {
        "Zoom" => has(&["leave", "leave meeting", "end meeting"]),
        "Teams" => has(&["leave", "hang up"]),
        "Slack" => has(&["leave huddle", "leave call"]),
        "Discord" => has(&["disconnect"]),
        "WhatsApp" | "FaceTime" | "Phone Link" | "BlueStacks" | "LDPlayer" => has(&["end call", "hang up"]),
        "Browser" => has(&["leave call", "leave meeting", "hang up"]),
        _ => false,
    };
    mic && end
}

#[cfg(not(any(windows, target_os = "macos")))]
pub(super) fn sample_with_tracking(_tracking: bool) -> Result<DetectionSample, String> {
    Ok(DetectionSample::default())
}

#[cfg(windows)]
pub(super) fn sample_with_tracking(tracking: bool) -> Result<DetectionSample, String> {
    use windows::Win32::{
        Foundation::CloseHandle,
        System::{Com::*, Threading::*, Variant::VARIANT},
        UI::{Accessibility::*, Shell::*},
    };
    use windows_core::PWSTR;
    unsafe fn executable(pid: u32) -> Option<String> {
        unsafe {
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
            let mut buf = vec![0u16; 32768];
            let mut len = buf.len() as u32;
            let result = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut len);
            let _ = CloseHandle(h);
            result.ok()?;
            Some(String::from_utf16_lossy(&buf[..len as usize]).rsplit('\\').next()?.to_lowercase())
        }
    }
    let audio = super::native_activity_snapshot()?;
    let input_pids: BTreeSet<u32> =
        audio.sessions.iter().filter(|s| s.input_active).map(|s| s.process_id).collect();
    let notifications =
        unsafe { SHQueryUserNotificationState().map(|s| s == QUNS_ACCEPTS_NOTIFICATIONS).unwrap_or(false) };
    if input_pids.is_empty() && !tracking {
        return Ok(DetectionSample {
            notifications,
            diagnostics: DetectionDiagnostics {
                microphone_active: Some(false),
                notifications_allowed: notifications,
                ..Default::default()
            },
            ..Default::default()
        });
    }
    let audio_apps: BTreeSet<String> = audio
        .sessions
        .iter()
        .filter(|s| s.input_active)
        .filter_map(|s| unsafe { executable(s.process_id) })
        .collect();
    unsafe {
        CoInitializeEx(None, COINIT_MULTITHREADED).ok().map_err(|e| e.to_string())?;
    }
    struct Guard;
    impl Drop for Guard {
        fn drop(&mut self) {
            unsafe {
                CoUninitialize();
            }
        }
    }
    let _guard = Guard;
    let result = (|| -> windows_core::Result<DetectionSample> {
        unsafe {
            // The default provider timeout is 20 seconds. A stale background window
            // must not delay a meeting prompt by an entire call-control scan.
            let automation: IUIAutomation2 = CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER)?;
            automation.SetConnectionTimeout(500)?;
            automation.SetTransactionTimeout(1000)?;
            let started = std::time::Instant::now();
            let root = automation.GetRootElement()?;
            let window_cache = automation.CreateCacheRequest()?;
            window_cache.SetTreeScope(TreeScope_Element)?;
            window_cache.AddProperty(UIA_ProcessIdPropertyId)?;
            window_cache.AddProperty(UIA_IsOffscreenPropertyId)?;
            // Enumerate the desktop's direct children only, never its entire subtree.
            let windows = root.FindAllBuildCache(
                TreeScope_Children,
                &automation.CreateTrueCondition()?,
                &window_cache,
            )?;
            let button_condition = automation.CreatePropertyCondition(
                UIA_ControlTypePropertyId,
                &VARIANT::from(UIA_ButtonControlTypeId.0),
            )?;
            let visible_condition =
                automation.CreatePropertyCondition(UIA_IsOffscreenPropertyId, &VARIANT::from(false))?;
            let controls = automation.CreateAndCondition(&button_condition, &visible_condition)?;
            let button_cache = automation.CreateCacheRequest()?;
            button_cache.SetTreeScope(TreeScope_Element)?;
            button_cache.SetAutomationElementMode(AutomationElementMode_None)?;
            button_cache.AddProperty(UIA_NamePropertyId)?;
            let mut evidence = Vec::new();
            let mut diagnostics = DetectionDiagnostics {
                microphone_active: Some(!input_pids.is_empty()),
                microphone_apps: audio_apps
                    .iter()
                    .filter_map(|exe| app_name(exe).map(str::to_owned))
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect(),
                notifications_allowed: notifications,
                ..Default::default()
            };
            for n in 0..windows.Length()?.min(128) {
                if started.elapsed() >= std::time::Duration::from_secs(3) {
                    diagnostics.inspection_limited = true;
                    break;
                }
                // A closing or inaccessible window must not discard evidence from
                // other applications in this sample.
                let Ok(window) = windows.GetElement(n) else { continue };
                if window.CachedIsOffscreen().map(|v| v.as_bool()).unwrap_or(true) {
                    continue;
                }
                let Ok(pid) = window.CachedProcessId() else { continue };
                let pid = pid as u32;
                if pid == std::process::id() || pid == 0 {
                    continue;
                }
                let Some(exe) = executable(pid) else {
                    continue;
                };
                let Some(app) = app_name(&exe) else {
                    continue;
                };
                // Idle, unrelated applications cannot establish a new call. A tracked call
                // remains inspectable after muting so that silence never ends assistance.
                if !tracking && !audio_apps.contains(&exe) {
                    continue;
                }
                diagnostics.inspected_windows += 1;
                // Batch only visible button names, once per known app window. No document
                // text, URL, title, chat, screenshot or accessibility action is read/invoked.
                // Cached reads avoid hundreds of cross-process round trips and find deep
                // browser controls without the old arbitrary 12-level cutoff.
                let buttons = match window.FindAllBuildCache(TreeScope_Descendants, &controls, &button_cache)
                {
                    Ok(buttons) => buttons,
                    Err(_) => {
                        diagnostics.inspection_limited = true;
                        continue;
                    }
                };
                let count = buttons.Length()?;
                diagnostics.inspection_limited |= count > 512;
                let mut names = BTreeSet::new();
                for index in 0..count.min(512) {
                    if let Ok(button) = buttons.GetElement(index)
                        && let Ok(name) = button.CachedName()
                    {
                        names.insert(name.to_string().split(" (").next().unwrap_or("").trim().to_lowercase());
                    }
                }
                #[cfg(test)]
                println!(
                    "Candidate app={app}, controls={}, microphone={}, call_controls={}",
                    names.len(),
                    audio_apps.contains(&exe),
                    call_controls(app, &names)
                );
                if call_controls(app, &names) {
                    diagnostics.matched_calls.push(app.into());
                    evidence.push(Evidence {
                        key: format!("{pid}:{app}"),
                        app: app.into(),
                        audio: if app == "Browser" {
                            audio_apps.contains(&exe)
                        } else {
                            input_pids.contains(&pid)
                        },
                    });
                }
            }
            Ok(DetectionSample { evidence, notifications, diagnostics })
        }
    })();
    result.map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn e() -> Evidence {
        Evidence { key: "42:Zoom".into(), app: "Zoom".into(), audio: true }
    }
    #[test]
    fn requires_specific_controls_not_identity_or_media() {
        assert_eq!(app_name("zoom.exe"), Some("Zoom"));
        assert!(!call_controls("Zoom", &BTreeSet::from(["play".into(), "mute".into()])));
        assert!(call_controls("Zoom", &BTreeSet::from(["leave meeting".into(), "mute".into()])));
        assert!(!call_controls("Browser", &BTreeSet::from(["end".into(), "mute".into()])));
    }
    #[test]
    fn debounce_dismiss_reconnect_and_end_are_separate() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        assert!(d.pending.is_none());
        d.tick(5, &[e()], true, false);
        assert_eq!(d.pending.as_ref().unwrap().kind, "start");
        d.respond("42:Zoom", false);
        d.tick(50, &[], true, false);
        d.tick(80, &[e()], true, false);
        assert!(d.pending.is_none());
        d.tick(400, &[], true, false);
        d.tick(401, &[e()], true, false);
        d.tick(406, &[e()], true, false);
        d.respond("42:Zoom", true);
        d.tick(500, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(527, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().kind, "end");
    }
    #[test]
    fn per_app_disable_clears_pending_and_end_offer() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.suppress_apps(&["Zoom".into()]);
        assert!(d.pending.is_none());
        d.tick(150, &[], true, true);
        assert!(d.pending.is_none());
    }
    #[test]
    fn muted_microphone_with_call_controls_preserves_call() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.respond("42:Zoom", true);
        let mut muted = e();
        muted.audio = false;
        d.tick(200, &[muted], true, true);
        assert!(d.pending.is_none());
        d.tick(250, &[], true, true);
        assert!(d.pending.is_none());
    }
    #[test]
    fn transient_notification_suppression_defers_unanswered_suggestions() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        assert!(d.pending.is_some());
        d.tick(6, &[e()], false, false);
        assert!(d.pending.is_none());
        d.tick(10, &[e()], true, false);
        assert_eq!(d.pending.as_ref().unwrap().kind, "start");
        d.respond("42:Zoom", true);
        d.tick(131, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().kind, "end");
        d.tick(132, &[], false, true);
        assert!(d.pending.is_none());
        d.tick(133, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().kind, "end");
    }
    #[test]
    fn pause_defers_only_unanswered_suggestions() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.defer_pending();
        d.tick(10, &[e()], true, false);
        assert!(d.pending.is_some());
        d.respond("42:Zoom", false);
        d.defer_pending();
        d.tick(15, &[e()], true, false);
        assert!(d.pending.is_none());
    }
    #[test]
    fn notification_suppression_does_not_reset_user_dismissal() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.respond("42:Zoom", false);
        d.tick(10, &[e()], false, false);
        d.tick(15, &[e()], true, false);
        assert!(d.pending.is_none());
    }
    #[test]
    fn dnd_active_session_and_no_audio_suppress_start() {
        let mut d = Detector::default();
        let mut silent = e();
        silent.audio = false;
        d.tick(0, &[silent], true, false);
        assert!(d.calls.is_empty());
        d.tick(1, &[e()], false, false);
        d.tick(10, &[e()], false, false);
        assert!(d.pending.is_none());
        d.tick(15, &[e()], true, true);
        d.tick(20, &[e()], true, false);
        assert!(d.pending.is_none());
    }
}

#[cfg(all(test, windows))]
mod host_diagnostics {
    #[test]
    #[ignore = "requires an interactive Windows desktop; diagnostic only, not call certification"]
    fn windows_accessibility_sample() {
        let started = std::time::Instant::now();
        let sample = super::sample_with_tracking(true).unwrap();
        println!("Native sample duration: {}ms", started.elapsed().as_millis());
        println!("Native call diagnostic: {:?}", sample.diagnostics);
        println!("Active microphone matches: {}", sample.evidence.iter().filter(|e| e.audio).count());
    }
}

#[cfg(target_os = "macos")]
pub(super) fn sample_with_tracking(tracking: bool) -> Result<DetectionSample, String> {
    let (evidence, notifications) = super::macos::sample(tracking)?;
    let diagnostics = DetectionDiagnostics {
        matched_calls: evidence.iter().map(|e| e.app.clone()).collect(),
        notifications_allowed: notifications,
        ..Default::default()
    };
    Ok(DetectionSample { evidence, notifications, diagnostics })
}
