//! App settings, as JSON in the app's config directory
//! (`%APPDATA%\com.copilot.desktop\settings.json` on Windows). No token is here: the dev token
//! and the sign-in's refresh token live in the Windows Credential Manager (copilot_client::auth).
//!
//! Two environments, each with its own URLs: `local` (the docker compose stack and the
//! services on this machine, with the dev token) and `cloud` (a deployed stage, with the
//! browser sign-in: Google on GCP, Cognito on AWS). Switching does not lose the other's URLs.
//! Files from before the rename (`aws`, `aws_*_url`, `cognito`) still load.

use std::collections::BTreeMap;
use std::path::PathBuf;

use copilot_client::oauth::OAuthConfig;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::hotkeys;
use crate::layout::MonitorLayout;
use crate::release::ReleaseConfig;
use crate::release::schedule::UpdateMode;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Environment {
    #[default]
    Local,
    #[serde(alias = "aws")]
    Cloud,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    pub environment: Environment,
    /// Local: the gateway's `/v1/session` WebSocket URL and the API's `/v1` root.
    pub gateway_url: String,
    pub api_url: String,
    /// Cloud: the same for the deployed stage (`terraform output gateway_url api_url`).
    #[serde(alias = "aws_gateway_url")]
    pub cloud_gateway_url: String,
    #[serde(alias = "aws_api_url")]
    pub cloud_api_url: String,
    /// The deployed stage's sign-in, from its `GET /v1/auth/config` at the last sign-in.
    #[serde(alias = "cognito")]
    pub oauth: Option<OAuthConfig>,
    /// cpal device ids; None is the system default.
    pub mic_device: Option<String>,
    pub output_device: Option<String>,
    pub echo_cancellation: bool,
    /// Global hotkey for question.force, in Tauri's accelerator syntax. The overlay's other
    /// hotkeys are below; all are rebindable and checked for clashes (hotkeys.rs).
    pub hotkey: String,
    /// Hide or show the overlay without ending the session.
    pub toggle_overlay_hotkey: String,
    /// Let clicks pass through the overlay to the window underneath, and turn that off again.
    /// The off switch always keeps a working key (hotkeys::click_through_off).
    pub click_through_hotkey: String,
    pub click_through_off_hotkey: String,
    /// Step the overlay's opacity.
    pub opacity_up_hotkey: String,
    pub opacity_down_hotkey: String,
    /// The overlay's frame, opacity and view remembered per monitor, and the monitor it was
    /// last on so it reopens there (layout.rs). Managed by the app, not the settings screen.
    pub layouts: BTreeMap<String, MonitorLayout>,
    pub overlay_monitor: Option<String>,
    /// The overlay stays above other windows (the overlay's "Keep on top" toggle).
    pub overlay_on_top: bool,
    pub show_debug: bool,
    /// The session this app last had live, so a 4409 can offer to end it.
    pub last_session_id: Option<Uuid>,
    /// The profile the app acts for (ARCHITECTURE.md, "Profiles"): one of the account's, by
    /// id; None is the account itself ("Me"). Sent on every API call and session.
    pub profile: Option<String>,
    /// Release builds: install updates by themselves (when no session runs), or ask first.
    pub update_mode: UpdateMode,
    /// The version that last ran, and the update being installed when the app exited: for
    /// the "Updated to x.y.z" note after the restart (release::schedule::after_start).
    pub last_version: Option<String>,
    pub pending_update: Option<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            environment: Environment::Local,
            gateway_url: "ws://127.0.0.1:8081/v1/session".into(),
            api_url: "http://127.0.0.1:8080/v1".into(),
            cloud_gateway_url: String::new(),
            cloud_api_url: String::new(),
            oauth: None,
            mic_device: None,
            output_device: None,
            echo_cancellation: true,
            hotkey: "CommandOrControl+Shift+Space".into(),
            toggle_overlay_hotkey: "CommandOrControl+Shift+H".into(),
            click_through_hotkey: "CommandOrControl+Shift+T".into(),
            click_through_off_hotkey: hotkeys::CLICK_THROUGH_OFF_FALLBACK.into(),
            opacity_up_hotkey: "CommandOrControl+Shift+Up".into(),
            opacity_down_hotkey: "CommandOrControl+Shift+Down".into(),
            layouts: BTreeMap::new(),
            overlay_monitor: None,
            overlay_on_top: true,
            show_debug: false,
            last_session_id: None,
            profile: None,
            update_mode: UpdateMode::Ask,
            last_version: None,
            pending_update: None,
        }
    }
}

impl Settings {
    pub fn load(path: &PathBuf) -> Self {
        std::fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
    }

    pub fn save(&self, path: &PathBuf) -> Result<(), String> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let text = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        std::fs::write(path, text).map_err(|e| e.to_string())
    }

    /// Release builds talk to their stage only: the cloud environment with the embedded URLs,
    /// whatever the file says (a dev build's settings file may be there from before).
    pub fn pin_to_release(&mut self, release: &ReleaseConfig) {
        if self.cloud_api_url.trim() != release.api_url {
            // The sign-in settings belong to the stage they came from.
            self.oauth = None;
        }
        self.environment = Environment::Cloud;
        self.cloud_api_url = release.api_url.clone();
        self.cloud_gateway_url = release.gateway_url.clone();
    }

    /// The URLs the app uses now: (gateway, API).
    pub fn active_urls(&self) -> (&str, &str) {
        match self.environment {
            Environment::Local => (&self.gateway_url, &self.api_url),
            Environment::Cloud => (&self.cloud_gateway_url, &self.cloud_api_url),
        }
    }

    /// The gateway's session URL, naming the profile the session acts for, if any.
    pub fn session_url(&self) -> String {
        let url = self.active_urls().0.trim();
        // Only an id's characters reach the URL.
        let id: String = self
            .profile
            .as_deref()
            .unwrap_or("")
            .chars()
            .filter(|c| c.is_ascii_hexdigit() || *c == '-')
            .collect();
        if id.is_empty() {
            return url.to_string();
        }
        let sep = if url.contains('?') { '&' } else { '?' };
        format!("{url}{sep}profile={id}")
    }
}

/// A warning for the settings screen when a URL sends traffic in the clear to another
/// machine: Stage 0 in the cloud has no TLS and is only safe behind its IP allow-list.
pub fn insecure_url_warning(url: &str) -> Option<String> {
    let url = url.trim();
    if url.is_empty() {
        return None;
    }
    let (scheme, rest) = url.split_once("://")?;
    if matches!(scheme, "https" | "wss") {
        return None;
    }
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = host.rsplit_once('@').map_or(host, |(_, h)| h);
    let host = if host.starts_with('[') {
        host.split(']').next().unwrap_or("").trim_start_matches('[')
    } else {
        host.split(':').next().unwrap_or("")
    };
    if matches!(host, "localhost" | "127.0.0.1" | "::1") {
        return None;
    }
    Some(format!(
        "{url} is not HTTPS: your sign-in token, documents and call text travel unencrypted. \
         Use it only for the Stage 0 test behind its IP allow-list."
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sessions_name_the_profile() {
        let mut s =
            Settings { profile: Some("0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b".into()), ..Settings::default() };
        assert_eq!(
            s.session_url(),
            "ws://127.0.0.1:8081/v1/session?profile=0192f3a9-55c2-7b10-a4e1-1c2d3e4f5a6b"
        );
        // Only an id's characters reach the URL.
        s.profile = Some("x&y=z".into());
        assert_eq!(s.session_url(), "ws://127.0.0.1:8081/v1/session");
        s.profile = Some(String::new());
        assert_eq!(s.session_url(), "ws://127.0.0.1:8081/v1/session");
    }

    #[test]
    fn old_settings_files_still_load_as_local() {
        let old = r#"{"gateway_url":"ws://10.0.0.5:8081/v1/session","api_url":"http://10.0.0.5:8080/v1","hotkey":"F9"}"#;
        let s: Settings = serde_json::from_str(old).unwrap();
        assert_eq!(s.environment, Environment::Local);
        assert_eq!(s.active_urls(), ("ws://10.0.0.5:8081/v1/session", "http://10.0.0.5:8080/v1"));
        assert_eq!(s.session_url(), "ws://10.0.0.5:8081/v1/session");
        assert_eq!(s.hotkey, "F9");
        // A file without the field takes the default: ask before installing.
        assert_eq!(s.update_mode, UpdateMode::Ask);
        assert!(s.last_version.is_none() && s.pending_update.is_none());
        let cloud = Settings {
            environment: Environment::Cloud,
            cloud_gateway_url: "ws://34.1.2.3/v1/session".into(),
            cloud_api_url: "http://34.1.2.3/v1".into(),
            ..s
        };
        assert_eq!(cloud.active_urls().1, "http://34.1.2.3/v1");
    }

    #[test]
    fn aws_settings_files_load_as_the_cloud_environment() {
        let old = r#"{"environment":"aws","aws_gateway_url":"ws://h/v1/session","aws_api_url":"http://h/v1",
            "cognito":{"domain":"https://d.auth.us-east-1.amazoncognito.com","client_id":"c","scopes":["openid"]}}"#;
        let s: Settings = serde_json::from_str(old).unwrap();
        assert_eq!(s.environment, Environment::Cloud);
        assert_eq!(s.active_urls(), ("ws://h/v1/session", "http://h/v1"));
        assert_eq!(s.oauth.as_ref().unwrap().provider_name(), "Cognito");
        let saved = serde_json::to_value(&s).unwrap();
        assert_eq!(saved["environment"], "cloud");
        assert!(saved.get("cloud_api_url").is_some() && saved.get("aws_api_url").is_none());
    }

    #[test]
    fn a_release_build_pins_the_cloud_environment_to_its_stage() {
        let release = ReleaseConfig {
            stage: "Stage 0".into(),
            api_url: "http://34.1.2.3/v1".into(),
            gateway_url: "ws://34.1.2.3/v1/session".into(),
            update_url: "https://storage.googleapis.com/b/desktop/latest.json".into(),
        };
        let oauth = r#"{"domain":"https://accounts.google.com","client_id":"c","scopes":["openid"]}"#;
        // A dev build's file, on the local stack: switched to the stage, the old sign-in dropped.
        let mut s: Settings = serde_json::from_str(&format!(
            r#"{{"environment":"local","cloud_api_url":"http://10.0.0.9/v1","oauth":{oauth}}}"#
        ))
        .unwrap();
        s.pin_to_release(&release);
        assert_eq!(s.environment, Environment::Cloud);
        assert_eq!(s.active_urls(), ("ws://34.1.2.3/v1/session", "http://34.1.2.3/v1"));
        assert!(s.oauth.is_none());
        // Already on the stage: the sign-in settings stay.
        let mut s: Settings = serde_json::from_str(&format!(
            r#"{{"environment":"cloud","cloud_api_url":"http://34.1.2.3/v1","oauth":{oauth}}}"#
        ))
        .unwrap();
        s.pin_to_release(&release);
        assert!(s.oauth.is_some());
    }

    #[test]
    fn overlay_comfort_fields_default_in_old_files_and_round_trip() {
        // A file from before overlay comfort has none of the new fields: they take defaults,
        // and the force-question hotkey the file does set is kept.
        let old = r#"{"hotkey":"F9"}"#;
        let s: Settings = serde_json::from_str(old).unwrap();
        assert_eq!(s.hotkey, "F9");
        assert_eq!(s.toggle_overlay_hotkey, "CommandOrControl+Shift+H");
        assert_eq!(s.click_through_off_hotkey, crate::hotkeys::CLICK_THROUGH_OFF_FALLBACK);
        assert!(s.layouts.is_empty());
        assert!(s.overlay_monitor.is_none());

        // The per-monitor layout and the chosen monitor survive a save/load round-trip.
        let mut s = Settings::default();
        s.layouts.insert(
            "DISPLAY1".into(),
            MonitorLayout { x: 40, y: 50, w: 480, h: 640, opacity: 0.7, compact: true },
        );
        s.overlay_monitor = Some("DISPLAY1".into());
        let text = serde_json::to_string(&s).unwrap();
        let back: Settings = serde_json::from_str(&text).unwrap();
        assert_eq!(back, s);
        assert_eq!(back.layouts["DISPLAY1"].opacity, 0.7);
    }

    #[test]
    fn plain_http_to_another_machine_gets_a_warning() {
        assert!(insecure_url_warning("http://ec2-1-2-3-4.compute-1.amazonaws.com/v1").is_some());
        assert!(insecure_url_warning("ws://10.0.0.5:8081/v1/session").is_some());
        assert!(insecure_url_warning("https://api.example.com/v1").is_none());
        assert!(insecure_url_warning("wss://rt.example.com/v1/session").is_none());
        assert!(insecure_url_warning("http://127.0.0.1:8080/v1").is_none());
        assert!(insecure_url_warning("ws://localhost:8081/v1/session").is_none());
        assert!(insecure_url_warning("http://[::1]:8080/v1").is_none());
        assert!(insecure_url_warning("").is_none());
    }
}
