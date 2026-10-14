//! App settings, as JSON in the app's config directory
//! (`%APPDATA%\com.copilot.desktop\settings.json` on Windows). No token is here: the dev token
//! and the Cognito refresh token live in the Windows Credential Manager (copilot_client::auth).
//!
//! Two environments, each with its own URLs: `local` (the docker compose stack and the
//! services on this machine, with the dev token) and `aws` (a deployed stage, with Cognito
//! sign-in). Switching does not lose the other's URLs.

use std::path::PathBuf;

use copilot_client::cognito::CognitoConfig;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Environment {
    #[default]
    Local,
    Aws,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    pub environment: Environment,
    /// Local: the gateway's `/v1/session` WebSocket URL and the API's `/v1` root.
    pub gateway_url: String,
    pub api_url: String,
    /// AWS: the same for the deployed stage (`terraform output gateway_url api_url`).
    pub aws_gateway_url: String,
    pub aws_api_url: String,
    /// The deployed stage's sign-in, from its `GET /v1/auth/config` at the last sign-in.
    pub cognito: Option<CognitoConfig>,
    /// cpal device ids; None is the system default.
    pub mic_device: Option<String>,
    pub output_device: Option<String>,
    pub echo_cancellation: bool,
    /// Global hotkey for question.force, in Tauri's accelerator syntax.
    pub hotkey: String,
    pub show_debug: bool,
    /// The session this app last had live, so a 4409 can offer to end it.
    pub last_session_id: Option<Uuid>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            environment: Environment::Local,
            gateway_url: "ws://127.0.0.1:8081/v1/session".into(),
            api_url: "http://127.0.0.1:8080/v1".into(),
            aws_gateway_url: String::new(),
            aws_api_url: String::new(),
            cognito: None,
            mic_device: None,
            output_device: None,
            echo_cancellation: true,
            hotkey: "CommandOrControl+Shift+Space".into(),
            show_debug: false,
            last_session_id: None,
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

    /// The URLs the app uses now: (gateway, API).
    pub fn active_urls(&self) -> (&str, &str) {
        match self.environment {
            Environment::Local => (&self.gateway_url, &self.api_url),
            Environment::Aws => (&self.aws_gateway_url, &self.aws_api_url),
        }
    }
}

/// A warning for the settings screen when a URL sends traffic in the clear to another
/// machine: Stage 0 on AWS has no TLS and is only safe behind its IP allow-list.
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
    fn old_settings_files_still_load_as_local() {
        let old = r#"{"gateway_url":"ws://10.0.0.5:8081/v1/session","api_url":"http://10.0.0.5:8080/v1","hotkey":"F9"}"#;
        let s: Settings = serde_json::from_str(old).unwrap();
        assert_eq!(s.environment, Environment::Local);
        assert_eq!(s.active_urls(), ("ws://10.0.0.5:8081/v1/session", "http://10.0.0.5:8080/v1"));
        assert_eq!(s.hotkey, "F9");
        let aws = Settings {
            environment: Environment::Aws,
            aws_gateway_url: "ws://ec2-1-2-3-4.compute-1.amazonaws.com/v1/session".into(),
            aws_api_url: "http://ec2-1-2-3-4.compute-1.amazonaws.com/v1".into(),
            ..s
        };
        assert_eq!(aws.active_urls().1, "http://ec2-1-2-3-4.compute-1.amazonaws.com/v1");
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
