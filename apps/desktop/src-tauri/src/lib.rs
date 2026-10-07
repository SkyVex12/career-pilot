//! The Tauri shell: windows, commands, the global hotkey, and settings. All audio, network and
//! storage work happens in copilot_client (apps/desktop/core); the web UI renders what it
//! emits and what the API returns.
//!
//! Every UI event goes to all windows as `copilot://event`, and is kept for the current
//! session so a window opened mid-call (the overlay) can catch up with `snapshot`. Uploads of
//! the user's own audio report as `copilot://upload`.

mod settings;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use copilot_client::api::ApiClient;
use copilot_client::audio::aec::{EchoCanceller, NoEchoCancellation, WebRtcAec};
use copilot_client::audio::capture::{self, DeviceInfo, DeviceSource};
use copilot_client::audio::pipeline::Levels;
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::auth::{DevTokenProvider, OsSecretStore, SecretStore, TokenProvider};
use copilot_client::engine::{AudioParts, Engine, UiEvent};
use copilot_client::net::client::{self, ClientConfig, ClientStats, ConnectionState, StartParams};
use copilot_client::net::close::StopReason;
use copilot_client::protocol::Mode;
use copilot_client::recording::{self, MeAudioSink, NoRecording, UploadOutcome};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use settings::Settings;
use tauri::ipc::{InvokeBody, Request};
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use uuid::Uuid;

const EVENT: &str = "copilot://event";
const UPLOAD_EVENT: &str = "copilot://upload";
const HISTORY_MAX: usize = 20_000;
const CREDENTIAL_SERVICE: &str = "com.copilot.desktop";
/// Retries after a failed upload: about 11 minutes in all, past the 10 minutes post-call work
/// waits for the audio. What is still left is tried again at the next app start.
const RETRY_SCHEDULE: [Duration; 7] = [
    Duration::from_secs(5),
    Duration::from_secs(15),
    Duration::from_secs(30),
    Duration::from_secs(60),
    Duration::from_secs(120),
    Duration::from_secs(180),
    Duration::from_secs(240),
];

struct AppState {
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    secrets: Arc<dyn SecretStore>,
    tokens: Arc<DevTokenProvider>,
    engine: Mutex<Option<Arc<Engine>>>,
    history: Arc<Mutex<Vec<serde_json::Value>>>,
    /// Encrypted recordings of the user's own audio waiting for their upload.
    recordings_dir: PathBuf,
    /// The last upload outcome per session, for a post-call screen opened later.
    uploads: Mutex<HashMap<Uuid, UploadOutcome>>,
    /// One upload run at a time (after a call, at startup).
    upload_lock: tokio::sync::Mutex<()>,
}

impl AppState {
    fn engine(&self) -> Result<Arc<Engine>, String> {
        self.engine.lock().unwrap().clone().ok_or_else(|| "no session is running".into())
    }

    fn update_settings(&self, f: impl FnOnce(&mut Settings)) {
        let mut s = self.settings.lock().unwrap();
        f(&mut s);
        if let Err(e) = s.save(&self.settings_path) {
            tracing::warn!("could not save settings: {e}");
        }
    }

    fn api(&self) -> Result<ApiClient, String> {
        let url = self.settings.lock().unwrap().api_url.clone();
        let tokens: Arc<dyn TokenProvider> = self.tokens.clone();
        ApiClient::new(&url, tokens).map_err(|e| e.to_string())
    }
}

type Res<T> = Result<T, String>;

#[tauri::command]
fn get_settings(state: State<'_, AppState>) -> (Settings, bool) {
    (state.settings.lock().unwrap().clone(), state.tokens.is_set())
}

#[tauri::command]
fn save_settings(
    app: AppHandle,
    state: State<'_, AppState>,
    settings: Settings,
    dev_token: Option<String>,
) -> Res<()> {
    ApiClient::new(&settings.api_url, state.tokens.clone()).map_err(|e| e.to_string())?;
    if let Some(token) = dev_token {
        state.tokens.save(&token).map_err(|e| e.to_string())?;
    }
    let hotkey_changed = state.settings.lock().unwrap().hotkey != settings.hotkey;
    let last = state.settings.lock().unwrap().last_session_id;
    state.update_settings(|s| *s = Settings { last_session_id: last, ..settings.clone() });
    if hotkey_changed {
        register_hotkey(&app, &settings.hotkey)?;
    }
    Ok(())
}

#[derive(Serialize)]
struct Devices {
    inputs: Vec<DeviceInfo>,
    outputs: Vec<DeviceInfo>,
}

#[tauri::command]
async fn list_devices() -> Devices {
    tauri::async_runtime::spawn_blocking(|| Devices {
        inputs: capture::input_devices(),
        outputs: capture::output_devices(),
    })
    .await
    .unwrap_or(Devices { inputs: vec![], outputs: vec![] })
}

/// One Copilot API call for the UI's screens (documents, jobs, interviews, sessions, usage).
#[tauri::command]
async fn api_call(
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Res<Value> {
    state.api()?.call(&method, &path, body.as_ref()).await.map_err(|e| e.to_string())
}

/// A resume or job description from the UI: the file's bytes as the raw body, its kind and
/// name in the `x-kind` and `x-file-name` headers (URI-encoded).
#[tauri::command]
async fn upload_document(state: State<'_, AppState>, request: Request<'_>) -> Res<Value> {
    let InvokeBody::Raw(bytes) = request.body() else {
        return Err("expected the file's bytes".into());
    };
    let header = |name: &str| -> Res<String> {
        let raw =
            request.headers().get(name).and_then(|v| v.to_str().ok()).ok_or(format!("missing {name}"))?;
        Ok(percent_decode(raw))
    };
    let kind = header("x-kind")?;
    if !matches!(kind.as_str(), "resume" | "job_description") {
        return Err(format!("cannot upload a {kind}"));
    }
    let file_name = header("x-file-name")?;
    state.api()?.upload_document(&kind, &file_name, bytes.clone()).await.map_err(|e| e.to_string())
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let Some(b) = s.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(h, 16).ok())
        {
            out.push(b);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `DELETE /me`, then every local recording; the dev token stays (it is the gateway's).
#[tauri::command]
async fn delete_account(state: State<'_, AppState>) -> Res<Value> {
    let result = state.api()?.delete("/me").await.map_err(|e| e.to_string())?;
    let _guard = state.upload_lock.lock().await;
    recording::delete_all(&state.recordings_dir);
    state.uploads.lock().unwrap().clear();
    state.update_settings(|s| s.last_session_id = None);
    Ok(result)
}

/// The last upload outcome of a session's audio, if this run of the app tried one.
#[tauri::command]
fn upload_status(state: State<'_, AppState>, session_id: Uuid) -> Option<UploadOutcome> {
    state.uploads.lock().unwrap().get(&session_id).cloned()
}

/// Upload the recordings waiting on disk, retrying on the schedule; `at_start` also takes
/// recordings a crash left open.
fn spawn_uploads(app: &AppHandle, at_start: bool) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let state = app.state::<AppState>();
        let _guard = state.upload_lock.lock().await;
        let api = match state.api() {
            Ok(api) => api,
            Err(e) => return tracing::warn!("recordings not uploaded: {e}"),
        };
        let report = {
            let app = app.clone();
            move |sid: Uuid, outcome: &UploadOutcome| {
                tracing::info!("audio for session {sid}: {outcome:?}");
                app.state::<AppState>().uploads.lock().unwrap().insert(sid, outcome.clone());
                let _ = app.emit(UPLOAD_EVENT, serde_json::json!({ "session_id": sid, "outcome": outcome }));
            }
        };
        recording::upload_pending(
            &api,
            state.secrets.as_ref(),
            &state.recordings_dir,
            at_start,
            &RETRY_SCHEDULE,
            &report,
        )
        .await;
    });
}

#[derive(Debug, Deserialize)]
struct StartRequest {
    mode: Mode,
    job_id: Option<Uuid>,
    interview_id: Option<Uuid>,
    meeting_title: Option<String>,
    platform: String,
    store_transcript: bool,
}

/// Events are numbered per session (`n`), so a window that replays the snapshot applies only
/// the live events after it.
fn emit(app: &AppHandle, history: &Mutex<Vec<serde_json::Value>>, event: &UiEvent) {
    let Ok(event) = serde_json::to_value(event) else { return };
    let value = {
        let mut h = history.lock().unwrap();
        let value = serde_json::json!({ "n": h.len(), "event": event });
        if h.len() < HISTORY_MAX {
            h.push(value.clone());
        }
        value
    };
    let _ = app.emit(EVENT, value);
}

/// The recording of the user's own audio: only with transcript consent. If it cannot start,
/// the call goes on without it.
fn recorder(state: &AppState, store_transcript: bool) -> (Box<dyn MeAudioSink>, Option<String>) {
    match recording::sink_for_session(store_transcript, &state.recordings_dir, state.secrets.as_ref()) {
        Ok(sink) => (sink, None),
        Err(e) => (
            Box::new(NoRecording),
            Some(format!(
                "Your own audio is not recorded ({e}); the transcript will have only the other side."
            )),
        ),
    }
}

#[tauri::command]
async fn start_session(app: AppHandle, state: State<'_, AppState>, request: StartRequest) -> Res<()> {
    if state.engine.lock().unwrap().is_some() {
        return Err("a session is already running".into());
    }
    state.tokens.current().await.map_err(|e| e.to_string())?;
    let settings = state.settings.lock().unwrap().clone();
    state.history.lock().unwrap().clear();

    let vad = || -> Res<ChannelVad> {
        Ok(ChannelVad::new(Box::new(Silero::new().map_err(|e| e.to_string())?), VadParams::default()))
    };
    let aec: Box<dyn EchoCanceller> =
        if settings.echo_cancellation { Box::new(WebRtcAec::new()) } else { Box::new(NoEchoCancellation) };
    let (me_recorder, warning) = recorder(&state, request.store_transcript);
    let parts = AudioParts {
        source: Box::new(DeviceSource::new(settings.mic_device.clone(), settings.output_device.clone())),
        aec,
        them_vad: vad()?,
        me_vad: vad()?,
        recorder: me_recorder,
    };
    let config = ClientConfig::new(
        settings.gateway_url.clone(),
        StartParams {
            mode: request.mode,
            job_id: request.job_id,
            interview_id: request.interview_id,
            meeting_title: request.meeting_title,
            platform: request.platform,
            client_version: format!("desktop-{}", env!("CARGO_PKG_VERSION")),
            store_transcript: request.store_transcript,
        },
    );
    let sink = {
        let app = app.clone();
        let history = state.history.clone();
        Arc::new(move |event: UiEvent| {
            if let UiEvent::Connection { state: ConnectionState::Live { session_id, .. } } = &event {
                let sid = *session_id;
                app.state::<AppState>().update_settings(|s| s.last_session_id = Some(sid));
            }
            if let UiEvent::Stopped { reason: StopReason::Ended | StopReason::Replaced } = &event {
                app.state::<AppState>().update_settings(|s| s.last_session_id = None);
            }
            emit(&app, &history, &event);
        })
    };
    if let Some(message) = warning {
        sink(UiEvent::Warning { message });
    }
    let tokens: Arc<dyn TokenProvider> = state.tokens.clone();
    let engine = Engine::start(config, tokens, parts, sink).map_err(|e| e.to_string())?;
    *state.engine.lock().unwrap() = Some(engine.clone());

    // When the client stops (ended, replaced, no minutes, ...), stop the audio, clear up and
    // upload the user's own audio: the socket is closed for good.
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        engine.finish().await;
        {
            let state = app2.state::<AppState>();
            let mut slot = state.engine.lock().unwrap();
            if slot.as_ref().is_some_and(|e| Arc::ptr_eq(e, &engine)) {
                *slot = None;
            }
        }
        spawn_uploads(&app2, false);
    });
    if let Some(overlay) = app.get_webview_window("overlay") {
        let _ = overlay.show();
    }
    Ok(())
}

#[tauri::command]
async fn end_session(state: State<'_, AppState>) -> Res<()> {
    state.engine()?.request_end().await;
    Ok(())
}

/// The 4409 offer: end the session this app last had live, so a new one can start.
#[tauri::command]
async fn end_other_session(state: State<'_, AppState>) -> Res<String> {
    let (url, sid) = {
        let s = state.settings.lock().unwrap();
        (s.gateway_url.clone(), s.last_session_id)
    };
    let Some(sid) = sid else {
        return Err("The other session was not started from this app. Close it there, or wait two \
                    minutes after it lost its connection."
            .into());
    };
    let code = client::end_session(&url, state.tokens.as_ref(), sid).await?;
    state.update_settings(|s| s.last_session_id = None);
    Ok(format!("ended session {sid} (close {code:?})"))
}

#[tauri::command]
async fn force_question(state: State<'_, AppState>, text: Option<String>) -> Res<()> {
    state.engine()?.force_question(text).await;
    Ok(())
}

#[tauri::command]
async fn expand_answer(state: State<'_, AppState>, question_id: Uuid) -> Res<()> {
    state.engine()?.expand(question_id).await;
    Ok(())
}

#[tauri::command]
async fn send_feedback(
    state: State<'_, AppState>,
    question_id: Uuid,
    up: bool,
    reason: Option<String>,
) -> Res<()> {
    state.engine()?.feedback(question_id, up, reason).await;
    Ok(())
}

#[tauri::command]
async fn set_mode(state: State<'_, AppState>, mode: Mode) -> Res<()> {
    state.engine()?.set_mode(mode).await;
    Ok(())
}

#[tauri::command]
fn snapshot(state: State<'_, AppState>) -> (Vec<serde_json::Value>, bool) {
    (state.history.lock().unwrap().clone(), state.engine.lock().unwrap().is_some())
}

#[derive(Serialize)]
struct Live {
    running: bool,
    session_id: Option<Uuid>,
    levels: Option<Levels>,
    stats: Option<ClientStats>,
}

#[tauri::command]
fn live_status(state: State<'_, AppState>) -> Live {
    match state.engine.lock().unwrap().as_ref() {
        Some(e) => {
            Live { running: true, session_id: e.session_id(), levels: e.levels(), stats: Some(e.stats()) }
        }
        None => Live { running: false, session_id: None, levels: None, stats: None },
    }
}

#[tauri::command]
fn show_overlay(app: AppHandle, show: bool) -> Res<()> {
    let w = app.get_webview_window("overlay").ok_or("no overlay window")?;
    if show { w.show() } else { w.hide() }.map_err(|e| e.to_string())
}

fn register_hotkey(app: &AppHandle, accelerator: &str) -> Res<()> {
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    if accelerator.trim().is_empty() {
        return Ok(());
    }
    let shortcut: Shortcut = accelerator.parse().map_err(|e| format!("hotkey {accelerator:?}: {e}"))?;
    gs.register(shortcut).map_err(|e| format!("hotkey {accelerator:?}: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    // The hotkey forces a question when detection missed one.
                    let app = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let engine = app.state::<AppState>().engine.lock().unwrap().clone();
                        if let Some(engine) = engine {
                            engine.force_question(None).await;
                        }
                    });
                })
                .build(),
        )
        .setup(|app| {
            let dir = app.path().app_config_dir()?;
            let settings_path = dir.join("settings.json");
            let settings = Settings::load(&settings_path);
            let hotkey = settings.hotkey.clone();
            let secrets: Arc<dyn SecretStore> = Arc::new(OsSecretStore::new(CREDENTIAL_SERVICE));
            app.manage(AppState {
                settings: Mutex::new(settings),
                settings_path,
                tokens: Arc::new(DevTokenProvider::new(secrets.clone())),
                secrets,
                engine: Mutex::new(None),
                history: Arc::new(Mutex::new(Vec::new())),
                recordings_dir: app.path().app_local_data_dir()?.join("me-audio"),
                uploads: Mutex::new(HashMap::new()),
                upload_lock: tokio::sync::Mutex::new(()),
            });
            if let Err(e) = register_hotkey(app.handle(), &hotkey) {
                tracing::warn!("{e}");
            }
            // GET /me wakes the database (Aurora scales to zero in Stage 0) before the first
            // call; then any recording a failed upload or a crash left behind goes up.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Ok(api) = handle.state::<AppState>().api()
                    && let Err(e) = api.get("/me").await
                {
                    tracing::info!("GET /me at startup: {e}");
                }
                spawn_uploads(&handle, true);
            });
            WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("index.html#overlay".into()))
                .title("Copilot overlay")
                .inner_size(440.0, 620.0)
                .min_inner_size(320.0, 240.0)
                .always_on_top(true)
                .decorations(false)
                .skip_taskbar(true)
                .visible(false)
                .build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_settings,
            save_settings,
            list_devices,
            api_call,
            upload_document,
            delete_account,
            upload_status,
            start_session,
            end_session,
            end_other_session,
            force_question,
            expand_answer,
            send_feedback,
            set_mode,
            snapshot,
            live_status,
            show_overlay,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::percent_decode;

    #[test]
    fn file_names_are_uri_decoded() {
        assert_eq!(percent_decode("R%C3%A9sum%C3%A9%20Alex.pdf"), "Résumé Alex.pdf");
        assert_eq!(percent_decode("plain.md"), "plain.md");
        assert_eq!(percent_decode("bad%zz.txt"), "bad%zz.txt");
    }
}
