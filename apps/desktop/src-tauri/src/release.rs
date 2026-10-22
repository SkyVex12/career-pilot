//! Release builds (the `release` feature): the installer teammates use. The stage's URLs come
//! from apps/desktop/release.json, embedded at build time (build.rs); the local environment and
//! the dev token are gone, and the app checks for an update at start (the Tauri updater, whose
//! endpoint and public key are in the generated tauri.release.conf.json, from the same file).

use serde::{Deserialize, Serialize};

/// apps/desktop/release.json. `infra/gcp/scripts/stage.py desktop-config` writes the URLs
/// from `terraform output desktop`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReleaseConfig {
    /// Shown in the settings, e.g. "Stage 0 (Google Cloud)".
    pub stage: String,
    pub api_url: String,
    pub gateway_url: String,
    /// The update manifest (latest.json) in the public releases bucket.
    pub update_url: String,
}

#[cfg(feature = "release")]
pub fn config() -> Option<&'static ReleaseConfig> {
    use std::sync::OnceLock;
    static CONFIG: OnceLock<ReleaseConfig> = OnceLock::new();
    Some(CONFIG.get_or_init(|| {
        serde_json::from_str(include_str!(concat!(env!("OUT_DIR"), "/release.json")))
            .expect("release.json was checked by build.rs")
    }))
}

#[cfg(not(feature = "release"))]
pub fn config() -> Option<&'static ReleaseConfig> {
    None
}

/// An update found at start, for the banner (`copilot://update`, `update_status`).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
#[cfg_attr(not(feature = "release"), allow(dead_code))]
pub enum UpdateState {
    /// Not a release build, or the check has not finished.
    None,
    Available {
        version: String,
        notes: Option<String>,
    },
    Installing {
        version: String,
    },
    Failed {
        message: String,
    },
}

#[cfg(feature = "release")]
pub mod updater {
    use std::sync::Mutex;

    use tauri::{AppHandle, Emitter, Manager};
    use tauri_plugin_updater::{Update, UpdaterExt};

    use super::UpdateState;

    pub const EVENT: &str = "copilot://update";

    #[derive(Default)]
    pub struct Updates {
        pending: Mutex<Option<Update>>,
        state: Mutex<Option<UpdateState>>,
    }

    impl Updates {
        pub fn state(&self) -> UpdateState {
            self.state.lock().unwrap().clone().unwrap_or(UpdateState::None)
        }

        fn set(&self, app: &AppHandle, state: UpdateState) {
            *self.state.lock().unwrap() = Some(state.clone());
            let _ = app.emit(EVENT, state);
        }
    }

    /// At start: ask the releases bucket for a newer version. A failure (offline, the bucket
    /// unreachable) only logs; the app works on.
    pub fn check_at_start(app: &AppHandle) {
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let found = match app.updater() {
                Ok(u) => u.check().await,
                Err(e) => Err(e),
            };
            let updates = app.state::<Updates>();
            match found {
                Ok(Some(update)) => {
                    tracing::info!(
                        "update available: {} (running {})",
                        update.version,
                        update.current_version
                    );
                    let state = UpdateState::Available {
                        version: update.version.clone(),
                        notes: update.body.clone(),
                    };
                    *updates.pending.lock().unwrap() = Some(update);
                    updates.set(&app, state);
                }
                Ok(None) => tracing::info!("no update: {} is the newest", app.package_info().version),
                Err(e) => tracing::warn!("update check failed: {e}"),
            }
        });
    }

    /// Download, verify the signature, run the installer (passive) and restart into the new
    /// version.
    pub async fn install(app: &AppHandle) -> Result<(), String> {
        let updates = app.state::<Updates>();
        let update = updates.pending.lock().unwrap().take().ok_or("no update to install")?;
        updates.set(app, UpdateState::Installing { version: update.version.clone() });
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => app.restart(),
            Err(e) => {
                let message = format!("the update failed: {e}");
                updates.set(app, UpdateState::Failed { message: message.clone() });
                Err(message)
            }
        }
    }
}
