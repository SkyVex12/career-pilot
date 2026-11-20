//! Local call evidence and lifecycle. No audio capture, source content retention or networking.
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum CallPhase {
    Incoming,
    Active,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct Suggestion {
    pub id: String,
    pub platform: String,
    pub kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<CallPhase>,
}
#[derive(Clone, Debug)]
pub(super) struct Evidence {
    pub key: String,
    pub app: String,
    pub audio: bool,
    /// Authoritative call-state metadata, independent of microphone/audio routing.
    pub call_phase: Option<CallPhase>,
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
    id: String,
    app: String,
    phase: Option<CallPhase>,
    first: u64,
    last: u64,
    offered: bool,
    accepted: bool,
    end_offered: bool,
    retired: bool,
    suppressed: bool,
}
impl Tracked {
    fn owns(&self, suggestion: &Suggestion) -> bool {
        match suggestion.kind {
            "start" => suggestion.id == self.id,
            "end" => suggestion.id.strip_prefix("end:") == Some(self.id.as_str()),
            _ => false,
        }
    }
}
#[derive(Default)]
pub(super) struct Detector {
    calls: BTreeMap<String, Tracked>,
    sequence: u64,
    pub pending: Option<Suggestion>,
}
impl Detector {
    pub fn suppress_apps(&mut self, disabled: &[String]) {
        if self
            .pending
            .as_ref()
            .is_some_and(|s| self.calls.values().any(|call| call.owns(s) && disabled.contains(&call.app)))
        {
            self.pending = None;
        }
        for call in self.calls.values_mut() {
            call.suppressed = disabled.contains(&call.app);
            if call.suppressed {
                call.offered = true;
                call.end_offered = true;
            }
        }
    }
    pub fn tracking(&self) -> bool {
        !self.calls.is_empty()
    }
    /// Delivery may hide an unanswered prompt during a failed or empty adapter
    /// sample without changing its occurrence or the user's dismissal state.
    #[cfg_attr(not(any(windows, test)), allow(dead_code))]
    pub fn pending_has_evidence(&self, evidence: &[Evidence]) -> bool {
        self.pending.as_ref().is_some_and(|pending| {
            pending.kind == "end"
                || self
                    .calls
                    .iter()
                    .any(|(key, call)| call.id == pending.id && evidence.iter().any(|item| item.key == *key))
        })
    }
    /// Only healthy idle metadata may retire a call; errors/unknown state must
    /// not call this method. Pass active=false to forget a removed configuration
    /// without an end review. A retired identity is immediately reusable, while
    /// an accepted running session keeps its conservative end review.
    #[cfg_attr(not(any(windows, test)), allow(dead_code))]
    pub fn retire(&mut self, keys: &[String], now: u64, active: bool) {
        let mut authoritative_ended = false;
        for key in keys {
            let Some(mut call) = self.calls.remove(key) else { continue };
            authoritative_ended |= active && call.phase.is_some();
            if self.pending.as_ref().is_some_and(|s| s.id == call.id && s.kind == "start") {
                self.pending = None;
            }
            if call.accepted && active {
                self.sequence = self.sequence.wrapping_add(1);
                let retired_key = format!("retired:{}:{key}", self.sequence);
                // Wait a full quiet interval after confirmed retirement. Never end
                // assistance automatically, including for a rejected/missed call.
                call.last = now;
                call.retired = true;
                self.calls.insert(retired_key, call);
            } else if self.pending.as_ref().is_some_and(|s| call.owns(s)) {
                self.pending = None;
            }
        }
        if authoritative_ended {
            // A subsequent call may have reused the existing Copilot session
            // without accepting another start prompt. Review its end only after
            // the last confirmed retirement has also had a full quiet interval.
            for call in self.calls.values_mut().filter(|call| call.retired && call.accepted) {
                call.last = now;
            }
        }
    }
    pub fn defer_pending(&mut self) {
        if let Some(pending) = self.pending.take()
            && let Some(call) = self.calls.values_mut().find(|call| call.owns(&pending))
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
    /// A known connecting call must not trigger assistance yet, but it makes
    /// ending an older accepted session premature, including across an ADB outage.
    #[cfg_attr(not(any(windows, test)), allow(dead_code))]
    pub fn defer_end_review(&mut self, now: u64) {
        if self.pending.as_ref().is_some_and(|suggestion| suggestion.kind == "end") {
            self.defer_pending();
        }
        for call in self.calls.values_mut().filter(|call| call.accepted) {
            call.last = now;
        }
    }
    pub fn tick(&mut self, now: u64, evidence: &[Evidence], allowed: bool, active: bool) {
        if !allowed {
            self.defer_pending();
        }
        for e in evidence {
            if !self.calls.contains_key(&e.key) && !e.audio && e.call_phase.is_none() {
                continue;
            }
            if !self.calls.contains_key(&e.key) {
                self.sequence = self.sequence.wrapping_add(1);
            }
            let c = self.calls.entry(e.key.clone()).or_insert(Tracked {
                // Notification IDs can be reused by the next call. Never let an
                // old popup response target that new occurrence after retirement.
                id: if e.call_phase.is_some() {
                    format!("call:{}:{}", self.sequence, e.key)
                } else {
                    e.key.clone()
                },
                app: e.app.clone(),
                phase: e.call_phase,
                first: now,
                last: now,
                ..Default::default()
            });
            c.last = now;
            // CallStyle can change from ringing to ongoing using the same key.
            // Do not regress a confirmed active call on a delayed ringing sample.
            if c.phase != Some(CallPhase::Active) && e.call_phase.is_some() {
                c.phase = e.call_phase;
            }
            if let Some(pending) = self.pending.as_mut().filter(|s| s.id == c.id && s.kind == "start") {
                pending.phase = c.phase;
            }
            if active && !c.accepted {
                c.offered = true;
            }
            if allowed
                && !active
                && !c.offered
                && (c.phase == Some(CallPhase::Incoming) || now.saturating_sub(c.first) >= 5)
                && self.pending.is_none()
            {
                c.offered = true;
                self.pending = Some(Suggestion {
                    id: c.id.clone(),
                    platform: e.app.clone(),
                    kind: "start",
                    phase: c.phase,
                });
            }
        }
        // An ongoing/unknown authoritative occurrence can still be using the
        // current Copilot session. It must not receive an old call's end prompt.
        let authoritative_present =
            self.calls.values().any(|c| c.phase.is_some() && !c.retired && !c.suppressed);
        if authoritative_present {
            if self.pending.as_ref().is_some_and(|s| s.kind == "end") {
                self.defer_pending();
            }
            for call in self.calls.values_mut().filter(|call| call.accepted && call.retired) {
                call.last = now;
            }
        }
        for c in self.calls.values_mut() {
            if now.saturating_sub(c.last) >= 120
                && !authoritative_present
                && (c.phase.is_none() || c.retired)
                && c.accepted
                && active
                && !c.suppressed
                && !c.end_offered
                && allowed
                && self.pending.is_none()
            {
                c.end_offered = true;
                self.pending = Some(Suggestion {
                    id: format!("end:{}", c.id),
                    platform: c.app.clone(),
                    kind: "end",
                    phase: None,
                });
            }
        }
        if self.pending.as_ref().is_some_and(|s| {
            (s.kind == "end" && !active)
                || (s.kind == "start"
                    && (active
                        || self
                            .calls
                            .values()
                            .find(|call| call.id == s.id)
                            .is_none_or(|c| c.phase.is_none() && now.saturating_sub(c.last) >= 120)))
        }) {
            self.pending = None;
        }
        // An unavailable Android bridge is not evidence that a call finished.
        // Its bounded adapter explicitly retires occurrences after healthy idle
        // observations, preserving dismissals even across a prolonged outage.
        self.calls.retain(|_, c| {
            (!c.retired || active)
                && ((c.phase.is_some() && !c.retired)
                    || now.saturating_sub(c.last) < 300
                    || (c.accepted && active))
        });
    }
    pub fn respond(&mut self, id: &str, accept: bool) -> Option<Suggestion> {
        let s = self.pending.as_ref()?;
        if s.id != id {
            return None;
        }
        let s = self.pending.take()?;
        if let Some(c) = self.calls.values_mut().find(|call| call.owns(&s))
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
                        call_phase: None,
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
        Evidence { key: "42:Zoom".into(), app: "Zoom".into(), audio: true, call_phase: None }
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

    fn hushed(phase: CallPhase) -> Evidence {
        Evidence {
            key: "emulator-5554:notification-42:Hushed".into(),
            app: "Hushed".into(),
            audio: false,
            call_phase: Some(phase),
        }
    }

    #[test]
    fn incoming_metadata_prompts_immediately_without_claiming_audio() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        assert!(!incoming.audio);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let first = d.pending.clone().unwrap();
        assert_eq!(first.platform, "Hushed");
        assert_eq!(first.phase, Some(CallPhase::Incoming));
        assert_eq!(first.kind, "start");
        assert_ne!(first.id, incoming.key);
        d.tick(1, std::slice::from_ref(&incoming), true, false);
        assert_eq!(d.pending.as_ref(), Some(&first));
        assert_eq!(d.calls.len(), 1);
        let json = serde_json::to_value(&first).unwrap();
        assert_eq!(json["phase"], "incoming");
    }

    #[test]
    fn active_metadata_without_microphone_retains_debounce() {
        let mut d = Detector::default();
        d.tick(0, &[hushed(CallPhase::Active)], true, false);
        assert!(d.pending.is_none());
        d.tick(4, &[hushed(CallPhase::Active)], true, false);
        assert!(d.pending.is_none());
        d.tick(5, &[hushed(CallPhase::Active)], true, false);
        assert_eq!(d.pending.as_ref().unwrap().phase, Some(CallPhase::Active));
    }

    #[test]
    fn ringing_to_active_updates_one_prompt_and_never_regresses() {
        let mut d = Detector::default();
        d.tick(0, &[hushed(CallPhase::Incoming)], true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.tick(1, &[hushed(CallPhase::Active)], true, false);
        assert_eq!(d.pending.as_ref().unwrap().id, id);
        assert_eq!(d.pending.as_ref().unwrap().phase, Some(CallPhase::Active));
        d.tick(2, &[hushed(CallPhase::Incoming)], true, false);
        assert_eq!(d.pending.as_ref().unwrap().phase, Some(CallPhase::Active));
        d.respond(&id, false).unwrap();
        d.tick(10, &[hushed(CallPhase::Active)], true, false);
        d.tick(20, &[hushed(CallPhase::Active)], false, false);
        d.tick(25, &[hushed(CallPhase::Active)], true, false);
        assert!(d.pending.is_none());
    }

    #[test]
    fn retirement_expires_ringing_and_rearms_reused_notification_with_new_action_id() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let old_id = d.pending.as_ref().unwrap().id.clone();
        d.retire(std::slice::from_ref(&incoming.key), 2, false);
        assert!(d.pending.is_none());
        assert!(!d.tracking());
        assert!(d.respond(&old_id, true).is_none());
        d.tick(3, std::slice::from_ref(&incoming), true, false);
        let new_id = d.pending.as_ref().unwrap().id.clone();
        assert_ne!(old_id, new_id);
        assert!(d.respond(&old_id, true).is_none());
        assert_eq!(d.pending.as_ref().unwrap().id, new_id);
        assert!(d.respond(&new_id, false).is_some());
        d.retire(std::slice::from_ref(&incoming.key), 4, false);
        d.tick(5, &[incoming], true, false);
        assert!(d.pending.is_some());
    }

    #[test]
    fn retired_accepted_call_only_offers_end_after_conservative_interval() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&id, true).unwrap();
        d.tick(1, &[hushed(CallPhase::Active)], true, true);
        d.retire(std::slice::from_ref(&incoming.key), 2, true);
        assert!(d.tracking());
        assert!(d.pending.is_none());
        d.tick(121, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(122, &[], false, true);
        assert!(d.pending.is_none());
        d.tick(123, &[], true, true);
        let end = d.pending.as_ref().unwrap().clone();
        assert_ne!(end.id, id);
        assert_eq!(end.kind, "end");
        assert_eq!(end.platform, "Hushed");
        assert_eq!(end.phase, None);
        assert!(d.pending_has_evidence(&[]));
        // A delayed Start response cannot act on the later end-review prompt.
        assert!(d.respond(&id, true).is_none());
        assert_eq!(d.pending.as_ref(), Some(&end));
        d.respond(&end.id, false).unwrap();
        d.tick(200, &[], true, true);
        assert!(d.pending.is_none());
    }

    #[test]
    fn retiring_accepted_call_without_a_running_session_does_not_offer_end() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&id, true).unwrap();
        d.retire(&[incoming.key], 1, false);
        d.tick(200, &[], true, true);
        assert!(d.pending.is_none());
        assert!(!d.tracking());
    }

    #[test]
    fn incoming_respects_dnd_pause_and_existing_sessions() {
        let mut d = Detector::default();
        d.tick(0, &[hushed(CallPhase::Incoming)], false, false);
        assert!(d.pending.is_none());
        d.tick(1, &[hushed(CallPhase::Incoming)], true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.defer_pending();
        assert!(d.pending.is_none());
        d.tick(2, &[hushed(CallPhase::Incoming)], true, false);
        assert_eq!(d.pending.as_ref().unwrap().id, id);
        d.tick(3, &[hushed(CallPhase::Incoming)], true, true);
        assert!(d.pending.is_none());
        d.tick(4, &[hushed(CallPhase::Incoming)], true, false);
        assert!(d.pending.is_none());
        let mut already_active = Detector::default();
        already_active.tick(0, &[hushed(CallPhase::Incoming)], true, true);
        already_active.tick(1, &[hushed(CallPhase::Active)], true, false);
        assert!(already_active.pending.is_none());
    }

    #[test]
    fn explicit_category_disables_hushed_even_without_key_suffix() {
        let mut d = Detector::default();
        let mut incoming = hushed(CallPhase::Incoming);
        incoming.key = "opaque-android-call-key".into();
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        d.suppress_apps(&["Hushed".into()]);
        assert!(d.pending.is_none());
        d.tick(1, std::slice::from_ref(&incoming), true, false);
        assert!(d.pending.is_none());

        let mut accepted = Detector::default();
        accepted.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = accepted.pending.as_ref().unwrap().id.clone();
        accepted.respond(&id, true).unwrap();
        accepted.retire(&[incoming.key], 1, true);
        accepted.suppress_apps(&["Hushed".into()]);
        accepted.tick(200, &[], true, true);
        assert!(accepted.pending.is_none());
    }

    #[test]
    fn missing_adapter_evidence_can_hide_delivery_without_resetting_occurrence() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        assert!(!d.pending_has_evidence(std::slice::from_ref(&incoming)));
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        assert!(d.pending_has_evidence(std::slice::from_ref(&incoming)));
        assert!(!d.pending_has_evidence(&[e()]));
        assert!(!d.pending_has_evidence(&[]));
        // A failed adapter read does not tick or retire. Recovery uses the same
        // action identity and a previous user dismissal remains authoritative.
        d.tick(5, std::slice::from_ref(&incoming), true, false);
        assert_eq!(d.pending.as_ref().unwrap().id, id);
        d.respond(&id, false).unwrap();
        d.tick(6, std::slice::from_ref(&incoming), true, false);
        assert!(!d.pending_has_evidence(&[incoming]));
    }

    #[test]
    fn prolonged_unknown_metadata_preserves_pending_and_user_dismissal() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.tick(600, &[], true, false);
        assert_eq!(d.pending.as_ref().unwrap().id, id);
        assert!(!d.pending_has_evidence(&[]));
        d.tick(601, std::slice::from_ref(&incoming), true, false);
        assert_eq!(d.pending.as_ref().unwrap().id, id);
        d.respond(&id, false).unwrap();
        d.tick(1200, &[], true, false);
        d.tick(1201, std::slice::from_ref(&incoming), true, false);
        assert!(d.pending.is_none());
        assert_eq!(d.calls.len(), 1);
    }

    #[test]
    fn unavailable_metadata_does_not_claim_an_accepted_call_ended() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&id, true).unwrap();
        d.tick(600, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(601, &[hushed(CallPhase::Active)], true, true);
        assert!(d.pending.is_none());
        d.retire(&[incoming.key], 602, true);
        d.tick(721, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(722, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().kind, "end");
    }

    #[test]
    fn another_authoritative_call_defers_end_until_every_occurrence_retires() {
        let mut d = Detector::default();
        let first = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&first), true, false);
        let first_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&first_id, true).unwrap();
        d.retire(std::slice::from_ref(&first.key), 1, true);
        let second = hushed(CallPhase::Active);
        let mut third = second.clone();
        third.key = "emulator-5556:notification-42:Hushed".into();
        d.tick(2, &[second.clone(), third.clone()], true, true);
        // Neither later call requires another start acceptance while Copilot is
        // active. Unknown adapter reads must not make either occurrence ended.
        d.tick(600, &[], true, true);
        assert!(d.pending.is_none());
        d.retire(std::slice::from_ref(&second.key), 601, true);
        d.tick(800, &[], true, true);
        assert!(d.pending.is_none());
        d.retire(&[third.key], 801, true);
        d.tick(920, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(921, &[], true, true);
        let end = d.pending.as_ref().unwrap();
        assert_eq!(end.kind, "end");
        assert_eq!(end.id, format!("end:{first_id}"));
    }

    #[test]
    fn new_authoritative_call_withdraws_unanswered_end_review_then_rearms_it() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let start_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&start_id, true).unwrap();
        d.retire(std::slice::from_ref(&incoming.key), 1, true);
        d.tick(121, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        d.tick(122, &[hushed(CallPhase::Active)], true, true);
        assert!(d.pending.is_none());
        assert!(d.respond(&end_id, true).is_none());
        d.tick(400, &[], true, true);
        assert!(d.pending.is_none());
        d.retire(&[incoming.key], 401, true);
        d.tick(520, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(521, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().id, end_id);
        assert_eq!(d.pending.as_ref().unwrap().kind, "end");
    }

    #[test]
    fn connecting_call_withdraws_old_end_review_without_offering_a_start() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let start_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&start_id, true).unwrap();
        d.retire(std::slice::from_ref(&incoming.key), 1, true);
        d.tick(121, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        for now in [122, 200, 400] {
            // New CONNECTING call, continued transition, then confirmed retirement.
            d.defer_end_review(now);
            d.tick(now, &[], true, true);
            assert!(d.pending.is_none());
            assert!(d.respond(&end_id, true).is_none());
        }
        d.tick(519, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(520, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().id, end_id);
        d.respond(&end_id, false).unwrap();
        d.defer_end_review(521);
        d.tick(1000, &[], true, true);
        assert!(d.pending.is_none(), "explicit dismissal must remain dismissed");
    }

    #[test]
    fn connecting_call_also_defers_a_generic_desktop_call_end_review() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.respond("42:Zoom", true).unwrap();
        d.tick(125, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        d.defer_end_review(126);
        d.tick(126, &[], true, true);
        assert!(d.pending.is_none());
        assert!(d.respond(&end_id, true).is_none());
        d.tick(245, &[], true, true);
        assert!(d.pending.is_none());
        d.tick(246, &[], true, true);
        assert_eq!(d.pending.as_ref().unwrap().id, end_id);
    }

    #[test]
    fn new_authoritative_call_does_not_reset_an_explicit_end_review_dismissal() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let start_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&start_id, true).unwrap();
        d.retire(std::slice::from_ref(&incoming.key), 1, true);
        d.tick(121, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&end_id, false).unwrap();
        d.tick(122, &[hushed(CallPhase::Active)], true, true);
        d.retire(&[incoming.key], 123, true);
        d.tick(500, &[], true, true);
        assert!(d.pending.is_none());
    }

    #[test]
    fn disabling_category_clears_a_pending_end_action_with_its_distinct_id() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let start_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&start_id, true).unwrap();
        d.retire(&[incoming.key], 1, true);
        d.tick(121, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        d.suppress_apps(&["Hushed".into()]);
        assert!(d.pending.is_none());
        assert!(d.respond(&end_id, true).is_none());
        d.tick(500, &[], true, true);
        assert!(d.pending.is_none());
    }

    #[test]
    fn stopping_copilot_retires_old_end_actions_before_an_unrelated_session_starts() {
        let mut d = Detector::default();
        let incoming = hushed(CallPhase::Incoming);
        d.tick(0, std::slice::from_ref(&incoming), true, false);
        let start_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&start_id, true).unwrap();
        d.retire(&[incoming.key], 1, true);
        d.tick(121, &[], true, true);
        let end_id = d.pending.as_ref().unwrap().id.clone();
        d.tick(122, &[], true, false);
        assert!(d.pending.is_none());
        assert!(!d.tracking());
        assert!(d.respond(&end_id, true).is_none());
        d.tick(123, &[], true, true);
        assert!(d.pending.is_none());
    }

    #[test]
    fn disabled_authoritative_app_does_not_block_another_app_end_review() {
        let mut d = Detector::default();
        d.tick(0, &[e()], true, false);
        d.tick(5, &[e()], true, false);
        d.respond("42:Zoom", true).unwrap();
        d.tick(6, &[hushed(CallPhase::Active)], true, true);
        d.tick(200, &[], true, true);
        assert!(d.pending.is_none());
        d.suppress_apps(&["Hushed".into()]);
        d.tick(201, &[], true, true);
        let end = d.pending.as_ref().unwrap();
        assert_eq!(end.kind, "end");
        assert_eq!(end.platform, "Zoom");
        // Re-enabling metadata monitoring restores conservative deferral until
        // healthy idle, without issuing another start for the same occurrence.
        d.suppress_apps(&[]);
        d.tick(202, &[hushed(CallPhase::Active)], true, true);
        assert!(d.pending.is_none());
    }

    #[test]
    fn separate_emulator_instances_have_independent_call_occurrences() {
        let mut d = Detector::default();
        let first = hushed(CallPhase::Incoming);
        let mut second = first.clone();
        second.key = "emulator-5556:notification-42:Hushed".into();
        d.tick(0, &[first.clone(), second.clone()], true, false);
        let first_id = d.pending.as_ref().unwrap().id.clone();
        d.respond(&first_id, false).unwrap();
        d.tick(1, &[first.clone(), second.clone()], true, false);
        let second_id = d.pending.as_ref().unwrap().id.clone();
        assert_ne!(first_id, second_id);
        d.retire(&[first.key], 2, false);
        assert_eq!(d.pending.as_ref().unwrap().id, second_id);
        assert!(d.pending_has_evidence(&[second]));
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
