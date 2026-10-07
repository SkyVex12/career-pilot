//! App settings, as JSON in the app's config directory
//! (`%APPDATA%\com.copilot.desktop\settings.json` on Windows). The dev token is not here: it
//! lives in the Windows Credential Manager (copilot_client::auth).

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    /// The gateway's `/v1/session` WebSocket URL.
    pub gateway_url: String,
    /// The Copilot API's `/v1` root.
    pub api_url: String,
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
            gateway_url: "ws://127.0.0.1:8081/v1/session".into(),
            api_url: "http://127.0.0.1:8080/v1".into(),
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
}
